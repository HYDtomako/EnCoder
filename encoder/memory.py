"""Persistent memory: store / recall / extract / organize (v2 layout).

Layout (per DESIGN_memory_v2.md / xuigai.md):

    .MEMORY/index.json           JSON search index (machine-read, derived)
    .MEMORY/<category>.md        one file per category; topics clustered as
                                 ``## [title] desc`` blocks (human-editable truth)
    .MEMORY/.snapshots/<ts>/     snapshot of category files before destructive edits
    .MEMORY/.snapshots/legacy_*  backup of the pre-v2 layout, if migrated
    .MEMORY/.conflicts.json      contradictory / superseding memories awaiting the user

v2 changes vs. the old per-file-per-topic design:

- one file per *category* (long/short/work/personal_prefer/general), topics are
  ``## [title]`` blocks inside it -> no more ``user-xxx.md`` fragmentation.
- ``memory.md`` is replaced by ``index.json``, rebuilt from the ``.md`` files on
  every read, so hand edits are always picked up and never overwritten.
- ``keywords`` come from the topic's own title/desc (not bigram chunks of an
  unrelated sentence), so they always describe the stored content.
- contradictory / superseding memories are queued to ``.conflicts.json`` and the
  user is asked to keep-new / keep-old / merge, instead of silently piling up.

Guarantees (unchanged):

- Memory is *soft* context injected into the system prompt, never a hard
  constraint; the user's current request always wins.
- Every min-LLM call goes through :meth:`_semantic`, which returns None on any
  failure, and callers fall back to keyword / regex matching. Memory can never
  crash the agent.
"""

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES: tuple[str, ...] = ("long", "short", "work", "personal_prefer", "general")

# words meaning "now / this time / temporary" -> such content must NOT be stored
TEMPORARY_WORDS: tuple[str, ...] = (
    "现在", "本次", "暂时", "当前", "这几天", "刚才", "回头", "待会", "先放着",
    "稍后", "临时", "此刻", "这一次",
    "now", "this time", "temporarily", "for now", "right now", "at the moment",
)

# weak markers that a round may contain something durable worth remembering
STORE_HINTS: tuple[str, ...] = (
    "记住", "记得", "偏好", "习惯", "喜欢", "热爱", "决定", "因为", "以后",
    "总是", "从不", "一定要", "prefer", "remember", "like", "decide",
    "always", "never",
)

_MAX_RECALL_MEMORIES = 3
_MAX_RECALL_ENTRY_CHARS = 500
_MAX_RECALL_TOTAL_CHARS = 1500
_MAX_JUDGE_CHARS = 3000
_DUP_OVERLAP = 0.6        # keyword overlap ratio marking two topics/entries as duplicates
_FOLLOWUP_MAX_CHARS = 12  # shorter than this -> treat as a follow-up, reuse last block
_FOLLOWUP_TTL = 60        # seconds the last recall block stays reusable

_LEGACY_INDEX_FILENAME = "memory.md"
_CONFLICTS_FILENAME = ".conflicts.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _esc(v: str) -> str:
    """Flatten a value into a single line (used in .md entry/header lines)."""
    return str(v).replace("|", "｜").replace("\n", " ").strip()


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]", "-", (s or "").lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "memory"


def _id(seed: str) -> str:
    """Deterministic short hash -> stable id suffix (no randomness at parse time)."""
    h = 0
    for ch in (seed or ""):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return f"{h:08x}"


def _topic_id(title: str, category: str) -> str:
    return f"{_slug(title)}-{_id(category + ':' + title)[:4]}"


def _clean_title(s: str) -> str:
    """Strip characters that would break the ``## [title]`` header syntax."""
    return _esc(s or "").replace("[", "(").replace("]", ")").strip()


def _keywords(*parts: str) -> list[str]:
    """Whole-word keywords derived from the topic's own title/desc (so they
    always describe the stored content, fixing the old unrelated-keyword bug).
    CJK runs are split into 2-char words so recall/overlap still works."""
    seen: list[str] = []
    for p in parts:
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-]*|[一-鿿]{2,}", (p or "").lower()):
            w = m.group(0)
            if w[0].isascii():
                if len(w) >= 2 and w not in seen:
                    seen.append(w)
            else:
                # split a long CJK run into overlapping 2-char keywords
                grams = [w[i:i + 2] for i in range(len(w) - 1)] if len(w) > 2 else [w]
                for g in grams:
                    if g not in seen:
                        seen.append(g)
    return seen[:12]


@dataclass
class MemoryTopic:
    """One topic (a ``## [title]`` block). The ``.md`` file is the source of
    truth; this dataclass mirrors whatever is on disk."""
    id: str
    title: str
    category: str = "general"
    desc: str = ""
    keywords: list[str] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)  # [{"date", "text"}]
    created: str = ""
    updated: str = ""
    status: str = "normal"                              # "normal" | "needs-update"

    @property
    def searchable(self) -> str:
        """Lowercased text used for keyword matching."""
        body = " ".join(e.get("text", "") for e in self.entries)
        return f"{self.id} {self.title} {' '.join(self.keywords)} {self.desc} {body}".lower()

    # back-compat aliases for the v1 CLI/agent callers that used .name/.type
    @property
    def name(self) -> str:
        return self.title

    @property
    def type(self) -> str:
        return self.category


@dataclass
class MemoryResult:
    """Returned by extract so the caller can surface updates to the user."""
    stored: bool = False
    action: str = ""        # "new" | "append" | "conflict" | "skip"
    topic: str = ""         # title of the affected topic
    category: str = ""
    old: str = ""
    new: str = ""
    old_date: str = ""

    @property
    def is_notice(self) -> bool:
        return self.action == "conflict" and bool(self.old)


# ------------------------------------------------------------------------- md/
# Parsing and writing the category .md files. Each topic is a ``## [title] desc``
# H2 header followed by `- date  text` bullets. A blank line is ignored (blocks
# are delimited by the next ``##`` header, not by blank lines).

_TOPIC_HEAD_RE = re.compile(r"^##\s+\[([^\]]+)\]\s*(.*)$")
_ENTRY_RE = re.compile(r"^-\s+(.+)$")
_DATE_RE = re.compile(r"^((?:19|20)\d\d-\d{2}-\d{2})\s+(.*)$")


def _parse_md(category: str, text: str) -> list[MemoryTopic]:
    """Parse a category-file body into topic records."""
    topics: list[MemoryTopic] = []
    cur: MemoryTopic | None = None

    def _flush() -> None:
        nonlocal cur
        if cur is not None and cur.title:
            topics.append(cur)
        cur = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "":
            continue
        mt = _TOPIC_HEAD_RE.match(line.strip())
        if mt:
            _flush()
            title = _clean_title(mt.group(1))
            desc = (mt.group(2) or "").strip()
            cur = MemoryTopic(
                id=_topic_id(title, category),
                title=title,
                category=category,
                desc=desc,
                keywords=_keywords(title, desc),
                created=_today(),
                updated=_today(),
            )
            continue
        me = _ENTRY_RE.match(line.strip())
        if cur is not None and me:
            body = me.group(1).strip()
            dm = _DATE_RE.match(body)
            if dm:
                date, text_ = dm.group(1), dm.group(2).strip()
            else:
                date, text_ = _today(), body
            cur.entries.append({"date": date, "text": text_})
            cur.updated = date
    _flush()
    return topics


def _serialize(topics: list[MemoryTopic]) -> str:
    """Render topics back to the canonical .md form (symmetric with _parse_md)."""
    blocks: list[str] = []
    for t in topics:
        lines = [f"## [{t.title}] {t.desc}".rstrip()]
        for e in t.entries:
            lines.append(f"- {e.get('date', '')}  {e.get('text', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


class MemoryManager:
    """Stores, recalls, extracts, organizes file-based memories (DESIGN v2)."""

    def __init__(self, base_dir: str | Path = ".MEMORY", llm=None,
                 memory_model: str | None = None, max_entries: int = 10):
        self.base = Path(base_dir)
        self.index_path = self.base / "index.json"
        self.conflicts_path = self.base / _CONFLICTS_FILENAME
        self.llm = llm
        self.memory_model = memory_model or os.getenv("ENCODER_MEMORY_LLM")
        self.max_entries = max_entries
        self._memory_llm = None
        self._last_block: str | None = None
        self._last_block_ts = 0.0
        self._migrated = self._maybe_migrate_legacy()

    # ------------------------------------------------------------- LLM infra

    def memory_llm(self):
        """Lazy min-LLM: a dedicated cheap model if configured, else main ``llm``."""
        if self._memory_llm is None:
            model = self.memory_model
            if model and self.llm is not None and model != getattr(self.llm, "model", model):
                try:
                    from .llm import LLM
                    api_key = (os.getenv("ENCODER_MEMORY_API_KEY")
                               or os.getenv("ENCODER_API_KEY")
                               or os.getenv("OPENAI_API_KEY")
                               or os.getenv("DEEPSEEK_API_KEY") or "")
                    base_url = (os.getenv("ENCODER_MEMORY_BASE_URL")
                                or os.getenv("OPENAI_BASE_URL") or None)
                    self._memory_llm = LLM(model=model, api_key=api_key, base_url=base_url)
                except Exception:
                    self._memory_llm = self.llm
            else:
                self._memory_llm = self.llm
        return self._memory_llm

    def _semantic(self, prompt: str) -> str | None:
        """One min-LLM call; returns text or None on any failure."""
        llm = self.memory_llm()
        if llm is None:
            return None
        try:
            resp = llm.chat([{"role": "user", "content": prompt}])
            return (resp.content or "").strip() or None
        except Exception:
            return None

    # -------------------------------------------------------------- top-level

    def _topics(self) -> list[MemoryTopic]:
        """Fresh list of topics parsed straight from the category .md files.

        There is no in-memory cache: every read re-parses disk, so hand edits
        are always honoured and never clobbered.
        """
        topics: list[MemoryTopic] = []
        for c in CATEGORIES:
            fp = self.base / f"{c}.md"
            if fp.exists():
                topics.extend(_parse_md(c, fp.read_text(encoding="utf-8", errors="replace")))
        return topics

    def list_meta(self) -> list[MemoryTopic]:
        return self._topics()

    def show(self, name: str) -> str:
        """Human-readable dump of a topic (match by id, title or keyword)."""
        for t in self._topics():
            if t.id == name or t.title == name or name in t.keywords:
                lines = [
                    f"name: {t.title}  (id: {t.id})",
                    f"type: {t.category}",
                    f"created: {t.created}   updated: {t.updated}",
                    f"desc: {t.desc}",
                    f"keywords: {', '.join(t.keywords)}",
                ]
                for e in t.entries:
                    lines.append(f"- {e.get('date', '')}  {e.get('text', '')}")
                return "\n".join(lines)
        return f"(no memory '{name}')"

    # ------------------------------------------------------------- serialize

    def _write_index(self):
        """Persist index.json as a derived search index (never the truth)."""
        self.base.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 2,
            "built_at": _now(),
            "memories": [
                {
                    "id": t.id,
                    "file": f"{t.category}.md",
                    "category": t.category,
                    "title": t.title,
                    "desc": t.desc,
                    "keywords": t.keywords,
                    "entries": [{"date": e.get("date", ""), "text": e.get("text", "")}
                                for e in t.entries],
                    "created": t.created,
                    "updated": t.updated,
                    "status": t.status,
                }
                for t in self._topics()
            ],
        }
        self.index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def _read_category(self, category: str) -> str:
        fp = self.base / f"{category}.md"
        return fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""

    def _write_category(self, category: str, topics: list[MemoryTopic]):
        """Write one category's topics back to its file, then refresh the index."""
        fp = self.base / f"{category}.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(_serialize(topics), encoding="utf-8")
        try:
            self._write_index()
        except Exception:
            pass

    # ---------------------------------------------------------------- recall

    def recall(self, user_input: str, recent_messages=None) -> str | None:
        """Return a rendered memory block for the system prompt, or None.

        Order: reuse the previous block on a short follow-up, then min-LLM
        semantic match on the topics, then keyword/regex fallback.
        """
        try:
            if (self._last_block and len(user_input.strip()) <= _FOLLOWUP_MAX_CHARS
                    and (time.time() - self._last_block_ts) <= _FOLLOWUP_TTL):
                return self._last_block

            topics = self._topics()
            if not topics:
                return None

            ids = self._semantic_recall(user_input, topics)
            if not ids:
                ids = self._keyword_recall(user_input, topics)
            if not ids:
                return None

            block = self._render_block(ids)
            self._last_block = block
            self._last_block_ts = time.time()
            return block
        except Exception:
            return None

    def _semantic_recall(self, user_input: str, topics: list[MemoryTopic]) -> list[str]:
        lines = "\n".join(
            f"- {t.id} {t.title} · {t.desc} · keywords: {','.join(t.keywords)}"
            for t in topics)
        prompt = (
            "你是记忆检索器。下面每行是一条记忆（id/标题/desc/keywords）。\n"
            "根据用户输入返回最相关的记忆 id，最多 3 个，输出 JSON 数组如 [\"a\",\"b\"]，无匹配输出 []。\n"
            f"记忆索引：\n{lines}\n\n用户输入：{user_input}"
        )
        text = self._semantic(prompt)
        if not text:
            return []
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if not m:
            return []
        try:
            names = json.loads(m.group(0))
        except json.JSONDecodeError:
            names = re.findall(r'"([^"]+)"', m.group(0))
        valid = {t.id for t in topics}
        return [n for n in names if n in valid][:_MAX_RECALL_MEMORIES]

    def _keyword_recall(self, user_input: str, topics: list[MemoryTopic]) -> list[str]:
        keys = self._tokenize(user_input)
        if not keys:
            return []
        scored: list[tuple[int, MemoryTopic]] = []
        for t in topics:
            hit = sum(1 for k in keys if k in t.searchable)
            if hit:
                scored.append((hit, t))
        scored.sort(key=lambda x: (x[0], x[1].updated), reverse=True)
        return [t.id for _, t in scored[:_MAX_RECALL_MEMORIES]]

    def _render_block(self, ids: list[str]) -> str | None:
        by_id = {t.id: t for t in self._topics()}
        parts: list[str] = []
        for i in ids:
            t = by_id.get(i)
            if not t:
                continue
            brief = "\n".join(f"{e.get('date', '')} {e.get('text', '')}" for e in t.entries)
            if not brief:
                brief = t.desc or t.title
            parts.append(f"- [{t.category}] {t.title}（{t.updated}）：{brief[:_MAX_RECALL_ENTRY_CHARS]}")
        if not parts:
            return None
        block = "\n".join(parts)
        if self._load_conflicts():
            block += ("\n若用户当前说法与上述记忆冲突，先向用户说明之前的记忆内容，再询问是否更新。")
        if len(block) > _MAX_RECALL_TOTAL_CHARS:
            block = block[:_MAX_RECALL_TOTAL_CHARS] + "\n… (truncated)"
        return block

    # -------------------------------------------------------------- extract

    def extract_and_store(self, messages_slice, hint_scan: str | None = None) -> MemoryResult:
        """After a round, decide whether anything durable is worth remembering.

        Returns a :class:`MemoryResult`; ``result.is_notice`` is True when a
        contradiction was queued and should be surfaced to the user.
        """
        try:
            text = hint_scan or self._flatten_slice(messages_slice)
            if not text or len(text) < 40:
                return MemoryResult(action="skip")
            if any(w in text for w in TEMPORARY_WORDS):
                return MemoryResult(action="skip")
            if not any(h in text.lower() for h in STORE_HINTS):
                return MemoryResult(action="skip")

            decision = self._judge(text)
            if decision is None:
                # LLM unavailable/failed -> keyword fallback placement
                decision = self._keyword_placement(text)
                if decision is None:
                    return MemoryResult(action="skip")
            elif not decision.get("store"):
                return MemoryResult(action="skip")

            return self._apply_decision(decision, text)
        except Exception:
            return MemoryResult(action="skip")

    def _flatten_slice(self, messages) -> str:
        parts: list[str] = []
        for m in (messages or [])[-6:]:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if role in ("user", "assistant") and content:
                parts.append(f"{role}: {content[:800]}")
        return "\n".join(parts)[:_MAX_JUDGE_CHARS]

    # ---------------------------------------------------------------- judge

    def _judge(self, text: str) -> dict | None:
        """LLM decision: store?, topic title, category, optional update/conflict.

        Returns None when the LLM is unavailable or produced unparseable output
        (the caller then falls back to keyword placement).
        """
        topics = self._topics()
        existing = "\n".join(
            f"- [{t.category}] {t.title} :: {t.desc}" for t in topics) if topics else "(无)"
        prompt = (
            "判断这段对话有没有值得长期保存的内容（用户持久偏好、项目事实、关键决策、长期目标）。\n"
            "“本次/暂时/现在”等临时含义不算可存内容。\n"
            "若它是对现有某条记忆的补充、更新或矛盾陈述，update 字段给出："
            "new(全新) / append(同义补充) / conflict(矛盾或更替)。\n"
            "只输出 JSON（不要多余文字）：{\"store\": true/false, \"title\": \"主题名如 饮食偏好\", "
            "\"category\": \"long|short|work|personal_prefer|general\", "
            "\"desc\": \"一句话\", \"update\": \"new|append|conflict\"}。\n"
            f"现有记忆：\n{existing}\n\n对话：\n{text[:_MAX_JUDGE_CHARS]}"
        )
        resp = self._semantic(prompt)
        if not resp:
            return None
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            d.setdefault("update", "new")
            d.setdefault("title", "")
            d.setdefault("desc", "")
            d.setdefault("category", "general")
            return d
        except json.JSONDecodeError:
            return None

    def _keyword_placement(self, text: str) -> dict | None:
        """Fallback placement: append to the topic with the highest token overlap,
        otherwise suggest a brand-new topic in the closest category."""
        keys = self._tokenize(text)
        topics = self._topics()
        best, best_score = None, 0
        for t in topics:
            score = sum(1 for k in keys if k in t.searchable)
            if score > best_score:
                best, best_score = t, score
        if best is not None and best_score >= 2:
            return {"store": True, "title": best.title, "category": best.category,
                    "desc": text[:80], "update": "append", "_target_id": best.id}
        cat = "personal_prefer" if any(w in text.lower() for w in STORE_HINTS) else "general"
        return {"store": True, "title": "", "category": cat, "desc": text[:80],
                "update": "new"}

    # ---------------------------------------------------------------- apply

    def _apply_decision(self, decision: dict, text: str) -> MemoryResult:
        action = decision.get("update") or "new"
        title = _clean_title(decision.get("title") or "") or self._guess_title(text)
        category = decision.get("category") if decision.get("category") in CATEGORIES else "general"
        desc = _esc(decision.get("desc") or text[:80]) or text[:80]
        note = desc
        target = self._find_topic(title, decision.get("_target_id"))

        if action == "conflict" and target is not None:
            # queue an update for the user to resolve - never auto-overwrite
            last = target.entries[-1] if target.entries else {}
            self._queue_conflict(target, note, last)
            return MemoryResult(stored=False, action="conflict", topic=target.title,
                                category=target.category, old=last.get("text", ""),
                                new=note, old_date=last.get("date", ""))

        if target is not None:
            if self._entry_is_duplicate(target, note):
                return MemoryResult(stored=True, action="append", topic=target.title,
                                    category=target.category)
            self._snapshot_topic(target)
            entries = target.entries + [{"date": _today(), "text": note}]
            self._rewrite_entries(target, entries)
            return MemoryResult(stored=True, action="append", topic=target.title,
                                category=target.category, new=note)

        new_topic = MemoryTopic(
            id=_topic_id(title, category),
            title=title, category=category, desc=desc,
            keywords=self._keywords(title, desc),
            created=_today(), updated=_today(),
            entries=[{"date": _today(), "text": note}],
        )
        self._append_topic(new_topic)
        return MemoryResult(stored=True, action="new", topic=title,
                            category=category, new=note, old="", old_date=_today())

    def ingest_summary(self, summary: str) -> int:
        """Pull durable facts out of a context-compression summary. Returns the
        number of topics written. Skips the temporary-word gate: a summary is
        already condensed context, and the judge is asked for durable facts only.
        """
        if not summary or len(summary) < 20:
            return 0
        try:
            decision = self._judge_summary(summary)
            if decision is None or not decision.get("store"):
                return 0
            res = self._apply_decision(decision, summary)
            return 1 if res.stored else 0
        except Exception:
            return 0

    def _judge_summary(self, text: str) -> dict | None:
        topics = self._topics()
        existing = "\n".join(
            f"- [{t.category}] {t.title}" for t in topics) if topics else "无"
        prompt = (
            "一条对话压缩摘要含若干事实。挑出值得长期记忆的部分（持续偏好、项目事实、决策、目标），"
            "忽略一次性执行细节。\n"
            "只输出 JSON：{\"store\": true/false, \"title\": \"主题名\", "
            "\"category\": \"long|short|work|personal_prefer|general\", "
            "\"desc\": \"一句话\", \"update\": \"new|append|conflict\"}。\n"
            f"现有记忆：\n{existing}\n\n摘要：\n{text[:_MAX_JUDGE_CHARS]}"
        )
        resp = self._semantic(prompt)
        if not resp:
            return None
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            d.setdefault("update", "new")
            d.setdefault("title", "")
            d.setdefault("desc", "")
            d.setdefault("category", "general")
            return d
        except json.JSONDecodeError:
            return None

    # -------------------------------------------------------------- storage

    def _find_topic(self, term: str, explicit_id: str | None = None) -> MemoryTopic | None:
        topics = self._topics()
        if explicit_id:
            for t in topics:
                if t.id == explicit_id:
                    return t
        term = (term or "").strip().lower()
        if not term:
            return None
        for t in topics:
            if t.title.lower() == term:
                return t
        for t in topics:
            if term in t.title.lower() or any(term in k.lower() for k in t.keywords):
                return t
        return None

    def _append_topic(self, topic: MemoryTopic):
        topics = [t for t in self._topics() if t.category == topic.category]
        topics.append(topic)
        self._write_category(topic.category, topics)

    def _rewrite_entries(self, topic: MemoryTopic, entries: list[dict]):
        topics = self._topics()
        for t in topics:
            if t.id == topic.id and t.category == topic.category:
                t.entries = entries
                t.updated = self._max_date(entries)
                break
        self._write_category(topic.category, [t for t in topics if t.category == topic.category])

    def _remove_topic(self, topic: MemoryTopic):
        topics = [t for t in self._topics()
                  if not (t.id == topic.id and t.category == topic.category)]
        self._write_category(topic.category, topics)

    @staticmethod
    def _max_date(entries: list[dict]) -> str:
        dates = [e.get("date", "") for e in entries if e.get("date")]
        return max(dates) if dates else _today()

    def _snapshot_topic(self, topic: MemoryTopic):
        """Copy the topic's category file into a timestamped snapshot dir."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        d = self.base / ".snapshots" / ts
        d.mkdir(parents=True, exist_ok=True)
        src = self.base / f"{topic.category}.md"
        if src.exists():
            shutil.copy2(src, d / f"{topic.category}.md")

    def forget(self, name: str) -> bool:
        """Remove a topic (snapshot first) and its block from the file."""
        topic = self._find_topic(name)
        if topic is None:
            return False
        try:
            self._snapshot_topic(topic)
        except OSError:
            pass
        self._remove_topic(topic)
        return True

    # ------------------------------------------------------- conflict queue

    def _queue_conflict(self, topic: MemoryTopic, new_text: str, last: dict):
        pending = self._load_conflicts()
        pending.append({
            "id": topic.id, "title": topic.title, "category": topic.category,
            "detected": _now(),
            "old_entry": last.get("text", ""),
            "old_date": last.get("date", ""),
            "new_text": new_text,
        })
        self._save_conflicts(pending)

    def _load_conflicts(self) -> list[dict]:
        if not self.conflicts_path.exists():
            return []
        try:
            data = json.loads(self.conflicts_path.read_text(encoding="utf-8", errors="replace"))
            return data.get("pending", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _save_conflicts(self, pending: list[dict]):
        self.conflicts_path.parent.mkdir(parents=True, exist_ok=True)
        self.conflicts_path.write_text(
            json.dumps({"version": 1, "pending": pending}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def pending_notices(self) -> list[dict]:
        """Conflicts awaiting the user, for surfacing in the CLI/REPL."""
        return self._load_conflicts()

    # --------------------------------------------------------------- organize

    def organize(self, ask: Callable[[str, list[str]], str] | None = None) -> list[str]:
        """Long-term maintenance (v2): cap entries, merge duplicates, resolve
        conflicts, promote stable short/work to long. Returns change list."""
        changed: list[str] = []
        try:
            for topic in list(self._topics()):
                if len(topic.entries) > self.max_entries:
                    self._snapshot_topic(topic)
                    merged = self._summarize_entries(topic)
                    self._rewrite_entries(topic, merged)
                    changed.append(f"integrated {topic.title} ({len(topic.entries)} entries)")
            changed += self._merge_duplicates()
            changed += self._resolve_conflicts(ask)
            changed += self._promote_stable()
            self._write_index()
        except Exception as e:
            changed.append(f"organize error: {e}")
        return changed

    def _summarize_entries(self, topic: MemoryTopic) -> list[dict]:
        entries = [e.get("text", "") for e in topic.entries]
        prompt = ("把同一主题的以下记忆条目整合成 1-3 条更精炼的条目，保留关键事实、日期与决策。"
                  "逐行输出，每行以日期开头（原日期优先）。\n" + "\n".join(entries))
        resp = self._semantic(prompt)
        result: list[dict] = []
        if resp:
            for line in resp.splitlines():
                line = line.strip()
                if not line:
                    continue
                dm = _DATE_RE.match(line)
                if dm:
                    result.append({"date": dm.group(1), "text": dm.group(2).strip() or line})
                else:
                    result.append({"date": topic.updated, "text": line})
        if result:
            return result[:3]
        keep = [entries[0]]
        if len(entries) > 2:
            keep.append(entries[len(entries) // 2])
        keep.append(entries[-1])
        return [{"date": topic.updated, "text": k} for k in keep]

    def _merge_duplicates(self) -> list[str]:
        changed: list[str] = []
        topics = self._topics()
        dropped: set[str] = set()
        for i, a in enumerate(topics):
            if a.id in dropped:
                continue
            for b in topics[i + 1:]:
                if b.id in dropped or b.category != a.category:
                    continue
                if self._overlap(a, b) < _DUP_OVERLAP:
                    continue
                keep, drop = (a, b) if a.updated >= b.updated else (b, a)
                self._snapshot_topic(keep)
                self._snapshot_topic(drop)
                entries = keep.entries + [{"date": _today(), "text": f"（已并入 {drop.title} 的记忆）"}]
                self._rewrite_entries(keep, entries)
                self._remove_topic(drop)
                dropped.add(drop.id)
                changed.append(f"merged duplicate {drop.title} into {keep.title}")
        return changed

    def _overlap(self, a: MemoryTopic, b: MemoryTopic) -> float:
        return self._overlap_text(a.searchable, b.searchable)

    def _resolve_conflicts(self, ask) -> list[str]:
        pending = self._load_conflicts()
        if not pending:
            return []
        changed: list[str] = []
        still: list[dict] = []
        topics = {t.id: t for t in self._topics()}
        for c in pending:
            topic = topics.get(c.get("id"))
            if topic is None:
                still.append(c)
                continue
            q = (f"记忆 [{topic.title} ({topic.category})] 有更新/矛盾：\n"
                 f"旧（{c.get('old_date', '')}）：{c.get('old_entry', '')}\n"
                 f"新：{c.get('new_text', '')}\n保留哪个？")
            choice = ask(q, ["keep_new", "keep_old", "merge"]) if ask else "keep_new"
            if choice == "keep_old":
                changed.append(f"conflict {topic.title}: kept old, dropped new")
            else:
                self._snapshot_topic(topic)
                label = "（冲突合并）" if choice == "merge" else "（更新取代）"
                entries = topic.entries + [{"date": _today(), "text": f"{label}{c.get('new_text', '')}"}]
                self._rewrite_entries(topic, entries)
                changed.append(f"conflict {topic.title}: merged" if choice == "merge"
                               else f"conflict {topic.title}: replaced old with new")
        self._save_conflicts(still)
        return changed

    def _promote_stable(self) -> list[str]:
        changed: list[str] = []
        markers = ("长期", "以后", "always", "long-term", "目标", "goal")
        for t in list(self._topics()):
            if t.category in ("short", "work") and any(m in t.searchable for m in markers):
                self._snapshot_topic(t)
                self._remove_topic(t)
                t.category = "long"
                self._append_topic(t)
                changed.append(f"promoted {t.title} -> long")
        return changed

    # ------------------------------------------------------------------ util

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Word tokens for *matching*; CJK words are also split into bigrams to
        improve recall overlap (these bigrams are never stored as keywords)."""
        tokens: list[str] = []
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-]*|[一-鿿]{2,}", (text or "").lower()):
            w = m.group(0)
            tokens.append(w)
            if not w[0].isascii() and len(w) > 2:
                tokens.extend(w[i:i + 2] for i in range(len(w) - 1))
        return tokens

    def _keywords(self, *parts: str) -> list[str]:
        return _keywords(*parts)

    def _overlap_text(self, a: str, b: str) -> float:
        ta = set(self._tokenize(a))
        tb = set(self._tokenize(b))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / min(len(ta), len(tb))

    def _entry_is_duplicate(self, topic: MemoryTopic, note: str) -> bool:
        return any(self._overlap_text(e.get("text", ""), note) > _DUP_OVERLAP
                   for e in topic.entries)

    def _guess_title(self, text: str) -> str:
        s = _esc(text)
        m = re.match(r"^(.{2,14}?)[，。；：,.;:！？!?]?", s)
        return (m.group(1) if m else s[:14]) or s[:14]

    # -------------------------------------------------------------- migration

    def _maybe_migrate_legacy(self) -> bool:
        """Detect and rebuild a pre-v2 layout (memory.md index + per-category
        subdirectories) into the single-file-per-category layout."""
        legacy_index = self.base / _LEGACY_INDEX_FILENAME
        legacy_dirs = [self.base / c for c in CATEGORIES if (self.base / c).is_dir()]
        if not legacy_index.exists() and not legacy_dirs:
            return False

        # snapshot everything before rebuilding
        ts = time.strftime("%Y%m%d-%H%M%S")
        snap = self.base / ".snapshots" / f"legacy_{ts}"
        snap.mkdir(parents=True, exist_ok=True)
        for p in [legacy_index, *legacy_dirs]:
            if p.exists():
                try:
                    if p.is_dir():
                        shutil.copytree(p, snap / p.name)
                    else:
                        shutil.copy2(p, snap / p.name)
                except OSError:
                    pass

        entries = self._parse_legacy_index(legacy_index) if legacy_index.exists() else []
        entries += self._harvest_legacy_files(legacy_dirs)
        topics = self._legacy_to_topics(entries)
        by_cat: dict[str, list[MemoryTopic]] = {}
        for t in topics:
            by_cat.setdefault(t.category, []).append(t)
        for c, ts_ in by_cat.items():
            self._write_category(c, ts_)

        # remove the old layout now that the new one is in place
        try:
            if legacy_index.exists():
                legacy_index.unlink()
        except OSError:
            pass
        for d in legacy_dirs:
            try:
                shutil.rmtree(d)
            except OSError:
                pass
        self._write_index()
        return True

    def _parse_legacy_index(self, path: Path) -> list[dict]:
        out: list[dict] = []
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            fields: dict[str, str] = {}
            for part in line[2:].split(" | "):
                if ":" in part:
                    k, v = part.split(":", 1)
                    fields[k.strip()] = v.strip()
            content = fields.get("content", "") or fields.get("desc", "")
            if not content:
                continue
            out.append({
                "category": fields.get("type", "general"),
                "desc": fields.get("desc", "") or content,
                "content": content,
                "date": fields.get("updated") or fields.get("created") or _today(),
            })
        return out

    def _harvest_legacy_files(self, dirs: list[Path]) -> list[dict]:
        out: list[dict] = []
        for d in dirs:
            if not d.is_dir():
                continue
            category = d.name
            for fp in d.glob("*.md"):
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("- name:") or line.startswith("#"):
                        continue
                    if line.startswith("- "):
                        line = line[2:].strip()
                    out.append({"category": category, "desc": line[:80],
                                "content": line, "date": _today()})
        return out

    def _legacy_to_topics(self, entries: list[dict]) -> list[MemoryTopic]:
        topics: list[MemoryTopic] = []
        for e in entries:
            category = e["category"] if e["category"] in CATEGORIES else "general"
            content = _esc(e["content"] or e["desc"])
            if not content:
                continue
            placed = False
            for t in topics:
                if self._overlap_text(t.searchable, content) > _DUP_OVERLAP:
                    t.entries.append({"date": e.get("date", _today()), "text": content})
                    t.updated = e.get("date", t.updated)
                    placed = True
                    break
            if placed:
                continue
            title = self._guess_title(e.get("desc", content))
            topics.append(MemoryTopic(
                id=_topic_id(title, category),
                title=title, category=category,
                desc=_esc(e.get("desc", content)[:80]),
                keywords=self._keywords(title, e.get("desc", "")),
                created=e.get("date", _today()), updated=e.get("date", _today()),
                entries=[{"date": e.get("date", _today()), "text": content}],
            ))
        return topics
