"""Agent checkpoint - breakpoint recovery via an event log + state snapshots.

**This is not a git checkpoint / snapshot.** Nothing here stores file contents or
rolls the working tree back; files belong to git and ``.worktrees``. A checkpoint
captures the *agent's* recoverable state so a killed process can pick the work
back up:

- Agent State     messages, todos, a *view* of the persisted tasks, context index
- Execution State the unanswered tool_call, running teammates, a pending approval
- Environment     cwd, git HEAD, branch, dirty files, worktrees (fingerprint only)

Design (see ``design_checkpoint.md``)::

    .CHECKPOINT/<session_id>/
      events.jsonl    append-only truth: one JSON event per line
      cp_0001.json    materialized state snapshot (a view of the event log)
      index.jsonl     derived cache of snapshot metadata (rebuildable, no authority)
      head.json       current pointer; restore only moves this, never rewrites history
      archive/        compaction moves originals here -- never deletes

Two vocabularies, deliberately kept apart:

- an **event type** is an open set (message_added, tool_done, ...) that also
  carries the fields the future trace will need;
- a **trigger** is a closed set (turn_end, compress, tool, ...) that compaction
  filters milestones on. Adding an event type without declaring its trigger
  mapping silently loses milestones, so ``trigger_for`` is the single mapping.

Everything degrades: a checkpoint failure must never break the agent loop.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# the session-dir naming convention is shared with the saved-session store, so a
# checkpoint dir and a `~/.encoder/sessions/*.json` file read as the same session
from .session import _new_session_id, _normalize_session_id

BASE_DIRNAME = ".CHECKPOINT"

# -- policy knobs ----------------------------------------------------------- #
SOFT_MIN_EVENTS = 5         # soft trigger: snapshot after this many events ...
SOFT_MAX_INTERVAL = 60.0    # ... or after this many seconds, whichever comes first
MAX_CHECKPOINTS = 50        # above this, compact() runs automatically
KEEP_CHECKPOINTS = 10       # compact() always keeps this many recent snapshots
ERROR_EXPIRY_EVENTS = 200   # an unresolved error this many events old goes stale

#: Tools whose effects re-running the agent cannot undo. A snapshot after one of
#: these is worth its cost; after ``read_file`` / ``grep`` it is pure waste.
IRREVERSIBLE_TOOLS = frozenset({
    "write_file", "edit_file", "bash",
    "spawn_teammate", "review_teammate", "release_teammate", "integrate_results",
    "create_schedule", "delete_schedule",
})

#: Tools whose whole point is a judgement about someone else's work. A snapshot
#: in flight on one of these has to say *what* is being reviewed, or a restored
#: agent re-dispatches a teammate that is already sitting on the answer (v2 §11.3).
REVIEW_TOOLS = frozenset({"review_teammate", "integrate_results"})

#: Triggers that always snapshot, bypassing the throttle. ``error`` is here
#: because the *first* failure at a given spot is the "why am I stuck" moment a
#: throttled snapshot would miss; a repeat of the same failure degrades to soft
#: instead (see ``should_checkpoint``).
HARD_TRIGGERS = frozenset({
    "turn_end", "compress", "approval", "interrupt", "stage_done",
    "task_done", "rewind", "manual", "teammate", "error", "long",
})

#: Triggers that snapshot only once the throttle above has passed.
SOFT_TRIGGERS = frozenset({"tool", "state"})

#: Closed set of error reasons (v2 §11.2). A reason is a *judgement*, not the
#: tool's raw output: "old_string not found" is the symptom, "your file view is
#: stale" is the reason, and only the reason tells a restored agent what to do
#: first. Same discipline as ``trigger``: enumerable, so recovery can branch.
REASON_KINDS = frozenset({
    "stale_view", "not_found", "conflict", "permission", "env", "timeout", "unknown",
})

#: What a restored agent is being asked to continue (v2 §11.3). Closed set.
RESUME_KINDS = frozenset({"task", "review", "tool", "approval", "blocked"})

#: Inputs that mean "carry on with what you were doing" rather than a new
#: instruction. Matched after stripping punctuation and case.
CONTINUE_WORDS = frozenset({
    "继续", "继续吧", "继续执行", "接着", "接着做", "往下", "go on", "continue", "resume",
})

#: How much of a tool result the event log keeps. Successful output is a trail,
#: not a transcript -- but an *error* has to survive intact, because the whole
#: promise of ``last_error`` is that the agent can go back and read it.
_OUTPUT_LIMIT = 800
_ERROR_OUTPUT_LIMIT = 8000
_INPUT_LIMIT = 500

#: A call the model itself declared will be slow, in seconds (its own ``timeout``
#: argument). This is the best signal there is for ⑤: it is the model saying "this
#: may take a while", it costs nothing to read, and unlike a hard-coded command
#: list it cannot rot.
LONG_TIMEOUT_SECONDS = 60

#: Commands that are slow by nature, matched on the *first words* of a command
#: (so ``echo make`` is not a build). A floor under the timeout signal: a model
#: that does not bother setting a timeout on ``pip install`` should still get a
#: recovery point, because that is the call whose failure costs the most time.
LONG_COMMANDS = frozenset({
    "pip install", "pip3 install", "python -m pip", "python3 -m pip",
    "npm install", "npm ci", "npm run", "yarn add", "yarn install", "yarn build",
    "pnpm add", "pnpm install", "poetry add", "poetry install", "conda install",
    "apt-get install", "apt install", "brew install", "choco install",
    "winget install", "cargo build", "cargo test", "cargo install",
    "go build", "go test", "go mod", "docker build", "docker pull", "docker run",
    "git clone", "make", "cmake", "gradle", "mvn", "pytest", "tox",
})


def _command_segments(command: str) -> list[str]:
    """Split a shell line into the parts that each start a process.

    ``cd x && pip install y`` is an install, not a ``cd``; checking only the head
    of the whole line would miss exactly the shape people actually type.
    """
    return [p.strip() for p in re.split(r"&&|\|\||;|\n", command) if p.strip()]


def looks_long(tool: str, args: dict | None) -> bool:
    """Is this call likely to run long enough that a *before* marker pays off (⑤)?

    Pure, like ``should_checkpoint``: the decision is a function of the call, and
    the caller supplies the call. Only ``bash`` qualifies -- every other tool
    returns promptly, so marking them would be the "无脑保存" the design warns
    about.

    What this buys is narrow and worth stating: the marker is written *before* the
    call runs, so a command killed halfway (SIGKILL, a closed terminal, Ctrl+C
    inside the tool) leaves a recovery point that names what was in flight. It
    does **not** roll anything back -- ``.TASK/`` and the working tree are the
    facts, and a checkpoint has never pretended otherwise.
    """
    if tool != "bash":
        return False
    args = args or {}
    try:
        if float(args.get("timeout") or 0) >= LONG_TIMEOUT_SECONDS:
            return True
    except (TypeError, ValueError):
        pass
    for segment in _command_segments(str(args.get("command") or "")):
        low = " ".join(segment.lower().split())
        for wrapper in ("sudo ", "time ", "env "):
            low = low.removeprefix(wrapper)   # absent prefix is a no-op
        if any(low == c or low.startswith(c + " ") for c in LONG_COMMANDS):
            return True
    return False


#: Triggers compaction must never merge away -- each marks a point a human or the
#: model deliberately wanted to be able to come back to.
MILESTONE_TRIGGERS = frozenset({
    "turn_end", "compress", "approval", "interrupt", "stage_done",
    "task_done", "rewind", "manual", "long",
})

#: Triggers that also skip fingerprint de-duplication. These are *deliberate*
#: boundaries: "I was interrupted here" or "this task just finished" is worth
#: recording even when the derived state happens to look unchanged.
FORCE_TRIGGERS = frozenset({"manual", "interrupt", "task_done", "rewind", "approval"})

_TAIL_BYTES = 65536         # how much of events.jsonl to read to find the last seq


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _brief(text, limit: int) -> str:
    """Truncate a value for the event log (events are a trail, not a transcript)."""
    s = "" if text is None else str(text)
    s = s.strip()
    return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} chars)"


def strip_continue(text) -> str:
    """Peel every leading "carry on" marker off ``text``.

    The whole point is that ``wrap_continue(strip_continue(x))`` cannot stack:
    restoring the same checkpoint five times must yield one "继续：…", not five.
    Idempotence lives *here*, inside the wrapper, so it does not depend on a
    caller remembering not to wrap twice (review01 §1).
    """
    s = ("" if text is None else str(text)).strip()
    while s:
        for word in sorted(CONTINUE_WORDS, key=len, reverse=True):
            if s.startswith(word):
                s = s[len(word):].lstrip("：:，,。.、 \t\n")
                break
        else:
            return s
    return s


def wrap_continue(text) -> str:
    """The continuation prefix. ``wrap_continue(wrap_continue(x)) == wrap_continue(x)``."""
    return f"继续：{strip_continue(text)}"


def is_continue_only(text) -> bool:
    """Is this input just "carry on", with no instruction of its own?"""
    return strip_continue(text) == ""


def classify_error(tool: str, output: str, args: dict | None = None) -> dict:
    """Turn a raw failure into a reason a restored agent can act on (v2 §11.2).

    Rules first, model never: the common failures are recognisable, and paying
    an LLM call on every failure would be slow and expensive for nothing. What
    the rules cannot place stays ``unknown`` -- and ``unknown`` is the metric
    that says the rule set needs another kind, not a silent gap.
    """
    text = ("" if output is None else str(output)).strip()
    low = text.lower()
    args = args or {}

    if any(m in low for m in ("old_string", "old string", "not found in the file",
                              "string to replace not found", "no match found")):
        return {"kind": "stale_view",
                "text": "目标文件在本次读取之后被改动过（或匹配串已不唯一）"}
    if any(m in low for m in ("file has been modified", "has changed since",
                              "stale file", "modified since read", "已被修改")):
        return {"kind": "stale_view", "text": "文件在读取之后被改动过"}
    if any(m in low for m in ("merge conflict", "automatic merge failed", "conflict (",
                              "both modified", "rebase in progress", "you have divergent")):
        return {"kind": "conflict", "text": "与已有改动/worktree 冲突，需要先归并"}
    if any(m in low for m in ("permission denied", "access is denied", "eacces",
                              "operation not permitted", "拒绝访问")):
        return {"kind": "permission", "text": "权限不足（或被审批拦下）"}
    if any(m in low for m in ("command not found", "is not recognized as an internal",
                              "modulenotfounderror", "cannot find module",
                              "not installed", "no module named")):
        return {"kind": "env", "text": "环境缺依赖或命令不存在"}
    if any(m in low for m in ("timed out", "timeout", "deadline exceeded", "超时")):
        return {"kind": "timeout", "text": "执行超时"}
    if any(m in low for m in ("no such file or directory", "no such file")):
        # The one genuinely ambiguous phrase, and the tool disambiguates it: a
        # shell saying it means the *command* is missing, a file tool saying it
        # means the *path* is. Same words, different first move on recovery
        # (install it vs. create it), so it cannot be one kind.
        if tool in ("bash", "shell"):
            return {"kind": "env", "text": "命令或工作目录不存在"}
        return {"kind": "not_found", "text": "目标路径不存在"}
    if any(m in low for m in ("does not exist", "path not found",
                              "unknown tool", "bad arguments")):
        return {"kind": "not_found", "text": "路径/工具/参数不存在"}
    return {"kind": "unknown", "text": _brief(text.splitlines()[0] if text else tool, 200)}


# --------------------------------------------------------------------------- #
# Event - one line of the append-only log
# --------------------------------------------------------------------------- #

@dataclass
class Event:
    """A single thing that happened.

    ``name`` / ``tool`` / ``input`` / ``output`` / ``status`` / ``error`` /
    ``files`` / ``workspace`` exist for the trace (``design_trace.md``): the same
    stream answers "what did the agent do, with what, and did it work".
    """

    seq: int = 0
    ts: str = ""
    type: str = ""
    actor: str = "lead"          # lead | agent_N | subagent | integrator | system | user
    name: str = ""               # who/what this is about (teammate name, task id...)
    tool: str = ""
    input: str = ""
    output: str = ""
    status: str = ""             # ok | error | pending
    error: str = ""
    files: list = field(default_factory=list)
    workspace: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON form with empty fields dropped, so the log stays readable."""
        out = {}
        for k, v in asdict(self).items():
            if k == "seq" or v not in ("", None, [], {}):
                out[k] = v
        return out

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _repeat_of(event: Event) -> int:
    """How many times this same failure has now been seen (1 = the first).

    Read off the event rather than from manager state so that
    ``should_checkpoint`` stays a pure function of ``(event, stats)``.
    """
    try:
        return int((event.data or {}).get("repeat", 0) or 0) + 1
    except (TypeError, ValueError):
        return 1


def trigger_for(event: Event) -> str | None:
    """Map an event type to a trigger, or None when it can never justify a snapshot.

    This is the ONLY mapping between the two vocabularies; keep it exhaustive.
    """
    t = event.type
    if t == "tool_start":
        # ⑤: logged *before* a call that is likely to run long. The event only
        # exists for those, but the policy still reads the flag rather than
        # assuming it -- the trigger vocabulary is where policy lives, and an
        # assertion about the emitter belongs here where it is testable.
        return "long" if event.data.get("long") else None
    if t == "tool_done":
        # A failure is a "why am I stuck here" moment even for a read-only tool:
        # the point of that recovery point is that the agent re-orients there.
        if event.status == "error":
            return "error"
        # a failed tool may still have had side effects, so errors count too
        return "tool" if event.tool in IRREVERSIBLE_TOOLS else None
    if t == "todo_changed":
        return "state"
    if t == "task_changed":
        # only a real transition into completed is a milestone; rewriting an
        # already-completed task is just another state change
        if event.data.get("to") == "completed" and event.data.get("from") != "completed":
            return "task_done"
        return "state"
    if t == "teammate_changed":
        # spawn/release/integrate change the environment; a teammate's internal
        # work state does not (the snapshot is the Lead's state, not theirs)
        return "teammate" if event.data.get("action") in ("spawn", "release", "integrate") else None
    if t in ("compress", "approval", "interrupt", "turn_end", "manual", "rewind"):
        return t
    return None


@dataclass
class Stats:
    """What the policy needs to know about the recent past."""

    events_since: int = 0        # events logged since the last snapshot
    last_at: float = 0.0         # monotonic time of the last snapshot (0 = never)
    writing: bool = False        # a snapshot is being written right now


@dataclass(frozen=True)
class Decision:
    should: bool
    trigger: str = ""
    reason: str = ""


def should_checkpoint(event: Event, stats: Stats, *, enabled: bool = True,
                      now: float | None = None) -> Decision:
    """Decide whether ``event`` warrants a snapshot. Pure function, no I/O.

    Order matters: cheap gates first, then hard triggers (which bypass the
    throttle), then the soft-trigger throttle. Fingerprint de-duplication is NOT
    here -- it needs the collected state, so the manager applies it afterwards
    (with ``manual`` exempt: "this is a milestone" is worth recording even when
    nothing changed).
    """
    if not enabled:
        return Decision(False, reason="disabled")
    if stats.writing:
        return Decision(False, reason="busy")   # another thread is mid-write; coalesce

    trigger = trigger_for(event)
    if trigger is None:
        return Decision(False, reason="not-checkpointable")

    # The first failure at a spot is hard (it is the "why stuck" moment); hitting
    # the same failure again and again is a loop, and a loop must not be allowed
    # to write a snapshot per iteration.
    soft = trigger in SOFT_TRIGGERS or (trigger == "error" and _repeat_of(event) > 1)

    if soft:
        elapsed = (time.monotonic() if now is None else now) - stats.last_at
        if stats.events_since < SOFT_MIN_EVENTS and elapsed < SOFT_MAX_INTERVAL:
            return Decision(False, reason="throttled")

    return Decision(True, trigger, reason=f"{event.type}->{trigger}")


# --------------------------------------------------------------------------- #
# EventLog - append-only JSONL
# --------------------------------------------------------------------------- #

class EventLog:
    """Append-only event log: one JSON object per line, flushed on every append.

    A crash loses at most the last line; a half-written or corrupt line is
    skipped on read rather than raising (same tolerance as ``session.py``).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seq = self._last_seq()

    # -- reading ---------------------------------------------------------------

    def _last_seq(self) -> int:
        """Highest seq already on disk, read from the tail only."""
        if not self.path.exists():
            return 0
        try:
            with self.path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - _TAIL_BYTES))
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            return 0
        for line in reversed(chunk.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue          # torn final line
            if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
                return obj["seq"]
        return 0

    def read(self, from_seq: int = 0) -> list[Event]:
        """All parseable events with ``seq >= from_seq``."""
        if not self.path.exists():
            return []
        events: list[Event] = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue          # corrupt line: skip, never crash recovery
            if isinstance(obj, dict) and obj.get("seq", 0) >= from_seq:
                events.append(Event.from_dict(obj))
        return events

    @property
    def seq(self) -> int:
        return self._seq

    # -- writing ---------------------------------------------------------------

    def emit(self, event_type: str, actor: str = "lead", **fields) -> Event:
        """Append one event and return it with its assigned seq."""
        event = Event(ts=_now(), type=event_type, actor=actor, **fields)
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                    f.flush()
            except OSError:
                pass              # logging must never break the loop
        return event

    def archive(self, keep_from_seq: int, archive_dir: Path) -> Path | None:
        """Split the log at ``keep_from_seq``: older lines move to ``archive/``.

        Nothing is deleted. Events at or after the cut stay in ``events.jsonl``,
        because the surviving checkpoints still cite them (a restored handoff
        reads the event tail); a ``note`` marker records where the cut was, so a
        reader knows history continues in the archive file.
        """
        with self._lock:
            if not self.path.exists():
                return None
            try:
                text = self.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

            old_lines: list[str] = []
            new_lines: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    seq = json.loads(line).get("seq", 0)
                except (json.JSONDecodeError, AttributeError):
                    old_lines.append(line)      # unparseable: archive it, don't drop
                    continue
                (new_lines if seq >= keep_from_seq else old_lines).append(line)
            if not old_lines:
                return None

            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / f"events-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
            try:
                dest.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
                # Written without a ``seq`` on purpose. Reusing the last used seq
                # would put two events numbered the same in one log (the design's
                # monotonic-seq promise), and bumping the counter would leave the
                # marker -- now the lowest line in the file -- fooling the
                # backward tail scan into handing out an already-taken seq. A
                # marker is a signpost, not an event, so it carries no number.
                marker = {"ts": _now(), "type": "note", "actor": "system",
                          "data": {"compact": "events", "kept_from_seq": keep_from_seq,
                                   "archived": dest.name, "lines": len(old_lines)}}
                self.path.write_text(
                    "\n".join([json.dumps(marker, ensure_ascii=False)]
                              + new_lines) + "\n",
                    encoding="utf-8")
            except OSError:
                return None
            return dest


# --------------------------------------------------------------------------- #
# CheckpointManager
# --------------------------------------------------------------------------- #

@dataclass
class Restored:
    """Result of a restore: what the agent should become, plus what a human reads."""

    checkpoint_id: str
    messages: list[dict]
    todos: list[dict]
    handoff: str
    env_changes: list[str] = field(default_factory=list)
    resume: dict = field(default_factory=dict)
    last_error: dict | None = None
    drift: list[str] = field(default_factory=list)


def describe_error(err: dict | None) -> str:
    """One line for an unresolved failure: what failed, and why (v2 §11.2).

    ``reason.text`` is human prose, ``reason.kind`` is the closed-set token the
    code branches on, and both matter: the token is what a recovered session
    acts on ("stale_view" -> re-read first), the prose is what the user reads.
    """
    if not err:
        return ""
    task = err.get("task") or {}
    reason = err.get("reason") or {}
    what = task.get("target") or task.get("command") or ""
    line = f"{task.get('tool') or '?'}"
    if what:
        line += f" → {_brief(what, 80)}"
    line += f"  原因[{reason.get('kind', 'unknown')}]：{reason.get('text', '')}"
    repeat = int(err.get("repeat", 1) or 1)
    if repeat > 1:
        line += f"（同一处第 {repeat} 次）"
    if task.get("intent"):
        line += f"  当时在做：{_brief(task['intent'], 80)}"
    if int(err.get("seq", 0) or 0):
        line += f"  [seq={err['seq']}]"
    return line


def describe_resume(resume: dict | None) -> str:
    """One line for the pending work: what kind, what to run, how many times."""
    if not resume:
        return ""
    kind = resume.get("kind", "task")
    parts = [f"kind={kind}"]
    if resume.get("text"):
        parts.append(f"任务：{_brief(resume['text'], 80)}")
    nxt = resume.get("next") or {}
    if nxt.get("tool"):
        args = _brief(json.dumps(nxt.get("args") or {}, ensure_ascii=False), 100)
        parts.append(f"下一步：{nxt['tool']}({args})")
    generation = int(resume.get("generation", 0) or 0)
    if generation > 1:
        parts.append(f"已恢复 {generation} 次")
    return "  ".join(parts)


class CheckpointManager:
    """Owns the event log, the snapshots and the restore/compact operations.

    ``agent`` is the Lead agent whose state is snapshotted; teammate agents never
    get their own manager (their events are reported into the Lead's log with
    ``actor=<name>`` through ``TeamManager.on_event``).
    """

    def __init__(self, agent=None, base_dir: Path | str = BASE_DIRNAME,
                 session_id: str | None = None, enabled: bool = True,
                 keep: int = KEEP_CHECKPOINTS, max_checkpoints: int = MAX_CHECKPOINTS,
                 llm=None):
        self.agent = agent
        self.enabled = bool(enabled)
        self.base_dir = Path(base_dir)
        self.session_id = _normalize_session_id(session_id) if session_id \
            else self._resolve_session_id()
        self.keep = keep
        self.max_checkpoints = max_checkpoints
        self._llm = llm
        self._lock = threading.RLock()
        self._writing = False
        self._last_at = 0.0
        self._last_fingerprint = ""
        self._events_at_last = 0
        self._last_handoff = ""      # rendered on restore, consumed by the prelude
        self._last_summary = ""      # last compression summary (a context index)
        self._last_compressed_seq = 0
        self._files_seen: set[str] = set()   # file pointers gathered from events

        # -- digest: recent facts kept as events flow past --------------------- #
        # Everything here is derivable from the log, and IS re-derived from it at
        # startup (``_rebuild_digest``): "the process was killed and restarted" is
        # this feature's main scenario, so a digest that only ever lived in memory
        # would come back empty exactly when it matters most.
        self._last_user = ""         # the raw human request, prelude-free
        self._last_error: dict | None = None    # newest UNRESOLVED failure
        self._errors: list[dict] = []           # every failure since the last snapshot
        self._last_approval: dict | None = None
        self._explicit_next: dict | None = None  # a next step the model declared
        self._generation = 0         # times this task has been resumed
        self._resume_text = ""       # the task text that generation counts
        self._pending_resume: dict | None = None

        path = self.base_dir / self.session_id
        self.log = EventLog(path / "events.jsonl")
        if self.enabled:
            try:
                path.mkdir(parents=True, exist_ok=True)
                self._rebuild_digest()
            except OSError:
                self.enabled = False     # unwritable -> degrade to a no-op

    def _rebuild_digest(self, limit: int = 400) -> None:
        """Replay the tail of the log into the digest (called once, at startup).

        ``limit`` is a tail bound, not a correctness bound: the digest holds the
        *recent* past, and an error older than a few hundred events is stale by
        definition (``ERROR_EXPIRY_EVENTS``).
        """
        try:
            events = self.log.read()
        except Exception:
            return
        for event in events[-limit:]:
            self._digest_event(event)
        # the resume counter is carried by the snapshot chain, so it survives too
        head = self._head_id()
        if head:
            cp = self.load(head)
            short = ((cp or {}).get("meta", {}) or {}).get("resume") or {}
            try:
                self._generation = int(short.get("generation", 0) or 0)
            except (TypeError, ValueError):
                self._generation = 0

    # -- layout ----------------------------------------------------------------

    def _resolve_session_id(self) -> str:
        """Reuse the newest session dir so a restart continues the same timeline.

        The feature's whole promise is "the process was killed -- pick up where it
        stopped", and a restart that landed in a fresh, empty directory would show
        no recovery points and read as broken. So the newest ``.CHECKPOINT/`` dir
        is adopted instead, and an explicit ``session_id`` bypasses this entirely
        (``adopt()`` covers reaching a *specific* older dir's checkpoint by id).
        """
        if self.base_dir.exists():
            dirs = sorted(
                (d for d in self.base_dir.iterdir()
                 if d.is_dir() and d.name.startswith("session_")),
                key=lambda d: d.name,
            )
            if dirs:
                return dirs[-1].name
        return _new_session_id()

    @property
    def dir(self) -> Path:
        return self.base_dir / self.session_id

    @property
    def archive_dir(self) -> Path:
        return self.dir / "archive"

    def _index_path(self) -> Path:
        return self.dir / "index.jsonl"

    def _head_path(self) -> Path:
        return self.dir / "head.json"

    # -- recording -------------------------------------------------------------

    def record(self, event_type: str, actor: str = "lead", **fields) -> Event | None:
        """Log an event and snapshot if the policy says so. Never raises."""
        if not self.enabled:
            return None
        try:
            status = str(fields.get("status", "") or "")
            # The log is a trail, not a transcript -- with one exception: a
            # failure has to survive intact, because the promise of last_error is
            # that the agent can go back and read it. A user's own request is
            # never clipped either: it is the raw material of the resume text.
            if fields.get("output"):
                limit = _ERROR_OUTPUT_LIMIT if status == "error" else _OUTPUT_LIMIT
                fields["output"] = _brief(fields["output"], limit)
            if fields.get("input") and event_type in ("tool_done", "approval"):
                fields["input"] = _brief(fields["input"], _INPUT_LIMIT)

            event = self.log.emit(event_type, actor=actor, **fields)
            self._digest_event(event)
            self._maybe_snapshot(event)
            return event
        except Exception:
            return None            # checkpointing must never break the agent loop

    def _log_only(self, event_type: str, actor: str = "lead", **fields) -> Event | None:
        """Log a fact without letting it trigger a snapshot.

        For facts that must be recorded but must not *themselves* produce a
        recovery point -- the rewind below is the case: the snapshot and the
        event are deliberately not simultaneous, because the snapshot has to be
        taken after the state has moved (v2 §11.4).
        """
        if not self.enabled:
            return None
        try:
            event = self.log.emit(event_type, actor=actor, **fields)
            self._digest_event(event)
            return event
        except Exception:
            return None

    # -- digest ---------------------------------------------------------------- #

    def _digest_event(self, event: Event) -> None:
        """Fold one event into the running digest. The single place that does.

        Kept separate from ``record`` because startup replays the log through
        this same function -- so a fact that survives a live session and a fact
        that survives a restart can never drift apart.
        """
        for f in event.files or []:
            self._files_seen.add(str(f))
        for f in (event.data or {}).get("files") or []:
            self._files_seen.add(str(f))

        if event.type == "user_message":
            self._last_user = str((event.data or {}).get("raw") or event.input or "")
            return

        if event.type == "note":
            data = event.data or {}
            if data.get("compress_summary"):
                self._last_summary = str(data["compress_summary"])
                self._last_compressed_seq = int(data.get("at_seq", 0) or 0)
            return

        if event.type == "approval":
            self._last_approval = {
                "tool": event.tool, "command": (event.data or {}).get("command", ""),
                "reason": (event.data or {}).get("reason", ""), "seq": event.seq,
                "at": event.ts, "status": event.status,
            }
            if event.status != "pending":
                self._last_approval = None
            return

        if event.type == "manual":
            nxt = (event.data or {}).get("next")
            if nxt:
                self._explicit_next = {
                    "tool": nxt, "args": (event.data or {}).get("next_args") or {},
                    "reason": str((event.data or {}).get("reason", "") or ""),
                }

        if event.type in ("tool_done", "approval"):
            if event.status == "error":
                self._note_error(event)
            elif event.status == "ok":
                self._resolve_errors(event)

    def _task_of(self, event: Event) -> dict:
        """What the agent was *doing* when it failed -- tool, target, intent.

        The tool name and arguments only say what was called; ``intent`` is what
        it was called *for*, and that is what a restored agent has to pick up.
        """
        args = (event.data or {}).get("args") or {}
        if not isinstance(args, dict):
            args = {}
        target = (args.get("file_path") or args.get("path") or args.get("pattern")
                  or args.get("name") or "")
        command = str(args.get("command", "") or "")
        # The model's own words at its last deliberate checkpoint outrank
        # anything inferred: it knew why it was calling the tool, and that is
        # exactly the thing the tool call alone cannot express.
        intent = ""
        if self._explicit_next:
            intent = (self._explicit_next.get("reason", "")
                      or self._explicit_next.get("args", {}).get("intent", "")
                      or self._explicit_next.get("tool", ""))
        return {"tool": event.tool or event.name, "target": str(target),
                "command": _brief(command, 200), "intent": _brief(intent, 200)}

    def _note_error(self, event: Event) -> None:
        """Record a failure, and how many times this same one has now happened."""
        task = self._task_of(event)
        reason = classify_error(task["tool"], event.error or event.output,
                                (event.data or {}).get("args") or {})
        same = [e for e in self._errors
                if not e.get("resolved")
                and _error_key(e) == (task["tool"], task["target"], reason["kind"])]
        repeat = (max(int(e.get("repeat", 1) or 1) for e in same) + 1) if same else 1
        # handed to the policy through the event, so should_checkpoint can stay pure
        event.data = {**(event.data or {}), "repeat": repeat - 1}
        self._errors.append({
            "task": task, "reason": reason, "raw": _brief(event.error or event.output, 300),
            "actor": event.actor, "seq": event.seq, "at": event.ts,
            "repeat": repeat, "resolved": False, "resolved_by": 0,
        })
        self._errors = self._errors[-20:]     # a digest, not an archive
        self._last_error = self._errors[-1]

    def _resolve_errors(self, event: Event) -> None:
        """A success for the same tool+target retires that failure.

        Without this the handoff would re-read a failure that was already fixed,
        which is the "repeatedly fixing the same thing" the review asked about.
        """
        tool = event.tool or event.name
        target = self._task_of(event)["target"]
        for err in self._errors:
            if err.get("resolved"):
                continue
            if err["task"]["tool"] == tool and err["task"]["target"] == target:
                err["resolved"] = True
                err["resolved_by"] = event.seq
        self._last_error = next((e for e in reversed(self._errors)
                                 if not e.get("resolved")), None)

    def _maybe_snapshot(self, event: Event) -> str | None:
        decision = should_checkpoint(event, self._stats(), enabled=self.enabled)
        if not decision.should:
            return None
        return self.snapshot(trigger=decision.trigger, reason=decision.reason,
                             actor=event.actor,
                             # a model-chosen label rides on the event and
                             # becomes the checkpoint's displayed name
                             label=str(event.data.get("label", "") or ""))

    def _stats(self) -> Stats:
        with self._lock:
            return Stats(events_since=self.log.seq - self._events_at_last,
                         last_at=self._last_at, writing=self._writing)

    def mark_compressed(self, summary: str = "") -> None:
        """Remember where the transcript was truncated and what survived.

        Compression is the project's only irreversible context operation --
        ``ContextManager`` rewrites ``messages`` in place -- so the pre-compress
        snapshot plus this cursor is what makes the lost work_state recoverable.

        The summary is written to the log as a ``note`` (notes never trigger a
        snapshot, and ``tail_events`` skips them) because it is the one thing
        that survives a truncation: keeping it in memory alone meant a restart
        came back with ``summary: ""`` and no idea a compression had happened.
        """
        with self._lock:
            self._last_summary = summary or self._last_summary
            self._last_compressed_seq = self.log.seq
            at_seq = self._last_compressed_seq
        if summary:
            self.log.emit("note", actor="system",
                          data={"compress_summary": _brief(summary, 1000),
                                "at_seq": at_seq})

    def note_handoff(self, block: str) -> None:
        """Hold a handoff block until the next request's prelude consumes it."""
        self._last_handoff = block or ""

    def take_handoff(self) -> str:
        """Read-and-clear the pending handoff block (prelude channel)."""
        block, self._last_handoff = self._last_handoff, ""
        return block

    def resume_for_input(self, raw: str) -> str:
        """Rewrite a bare "carry on" into the pending task (v2 §11.1).

        Only a *bare* continuation is rewritten. A real new instruction outranks
        the pending work, and forcing "继续：" in front of it would fight the user
        -- the handoff still lists what was in flight, so nothing is lost.

        The rewrite is ``wrap_continue``, which strips before it wraps, so
        restoring the same checkpoint repeatedly can never stack prefixes.
        """
        pending = self._pending_resume
        if not pending or not pending.get("text"):
            return raw
        if not is_continue_only(raw):
            return raw
        self._pending_resume = None
        return wrap_continue(pending["text"])

    # -- snapshots -------------------------------------------------------------

    def snapshot(self, trigger: str = "manual", label: str = "", actor: str = "lead",
                 reason: str = "", force: bool = False) -> str | None:
        """Write a state snapshot. Returns the checkpoint id, or None if skipped.

        Skipped when the state fingerprint matches the previous snapshot, unless
        the trigger is a deliberate boundary (``FORCE_TRIGGERS``) or ``force`` is
        passed: "I was interrupted here" is worth recording even when nothing
        changed. The rule lives here rather than at the call sites so a caller
        cannot accidentally drop a milestone by forgetting ``force=True``.
        """
        if not self.enabled:
            return None
        force = force or trigger in FORCE_TRIGGERS
        with self._lock:
            if self._writing:
                return None
            self._writing = True
            try:
                state = self._collect_state()
                fingerprint = _fingerprint(state)
                if not force and fingerprint == self._last_fingerprint:
                    return None

                cp_id = self._next_id()
                # a short copy of the resume descriptor rides in meta so
                # ``list`` can mark the resumable points without loading a full
                # state file for each one -- that is what index.jsonl is for
                resume = state.get("execution", {}).get("resume") or {}
                meta = {
                    "id": cp_id,
                    "parent_id": self._head_id(),
                    "created_at": _now(),
                    "trigger": trigger,
                    "label": label or _default_label(trigger),
                    "seq": self.log.seq,
                    "reason": reason,
                    "compacted": False,
                    "replaces": [],
                    "resume": ({"kind": resume.get("kind", ""),
                                "text": _brief(resume.get("text", ""), 80),
                                "generation": resume.get("generation", 0)}
                               if resume else {}),
                }
                self._write_atomic(self.dir / f"{cp_id}.json",
                                   {"meta": meta, "state": state})
                self._append_index(meta)
                self._set_head(cp_id)
                self._last_fingerprint = fingerprint
                self._last_at = time.monotonic()
                self._events_at_last = self.log.seq
            except Exception:
                return None
            finally:
                self._writing = False

        if self.count() > self.max_checkpoints:
            self.compact()
        return cp_id

    # -- state collection ------------------------------------------------------

    def _collect_state(self) -> dict:
        """The three state layers, storing pointers rather than content."""
        return {
            "agent": {
                "messages": copy.deepcopy(getattr(self.agent, "messages", []) or []),
                "todos": self._todos(),
                "tasks": self._task_view(),
                "context": self._context_view(),
            },
            "execution": self._execution_view(),
            "env": self._env_view(),
        }

    def _todos(self) -> list[dict]:
        todos = getattr(self.agent, "todos", None)
        if todos is None:
            return []
        try:
            return [asdict(t) for t in todos.list()]
        except Exception:
            return []

    def _task_view(self) -> list[dict]:
        """A view of the persisted tasks -- ``.TASK/`` stays the source of truth.

        Frozen at snapshot time on purpose: it describes what the task list
        looked like then, which is exactly what a handoff needs.
        """
        tasks = getattr(self.agent, "tasks", None)
        if tasks is None:
            return []
        out = []
        try:
            for t in tasks.list():
                if t.state == "completed":
                    continue
                out.append({"task_id": t.task_id, "state": t.state,
                            "description": t.description, "assignee": t.assignee,
                            "priority": t.priority, "blockedBy": list(t.blockedBy or []),
                            "note": t.note or ""})
        except Exception:
            return []
        return out

    def _context_view(self) -> dict:
        """Indices for rebuilding context -- never the prompt itself."""
        from .context import estimate_tokens
        from .tools.edit import _changed_files

        messages = getattr(self.agent, "messages", []) or []
        agent_summary = getattr(self, "_last_summary", "")
        focus_task = ""
        focus_todo = ""
        for t in self._todos():
            if t.get("status") == "in_progress":
                focus_todo = t.get("todo_id", "")
                break
        for t in self._task_view():
            if t.get("state") == "in_progress":
                focus_task = t.get("task_id", "")
                break
        try:
            tokens = estimate_tokens(messages)
        except Exception:
            tokens = 0
        return {
            "summary": agent_summary,
            "compressed_at": getattr(self, "_last_compressed_seq", 0),
            "files": sorted(set(_changed_files) | set(self._event_files())),
            "focus": {"task": focus_task, "todo": focus_todo},
            "token_estimate": tokens,
        }

    def _event_files(self) -> list[str]:
        """Files touched according to the event log, accumulated as events arrive.

        ``_changed_files`` only tracks edits made through edit/write tools in
        this process; the log also catches a bash command that rewrote a file,
        which is exactly the case a restored agent needs warned about. Kept as a
        running set rather than re-reading the log, so a snapshot stays O(1).
        """
        return sorted(self._files_seen)

    def _execution_view(self) -> dict:
        messages = getattr(self.agent, "messages", []) or []
        running = []
        team = getattr(self.agent, "team", None)
        if team is not None:
            # live teammates only: worktree/branch/status exist nowhere else
            try:
                for t in getattr(team, "_teammates", {}).values():
                    if t.status == "ending":
                        continue
                    running.append({"name": t.name, "status": t.status,
                                    "task_id": t.current_task_id,
                                    "branch": t._branch, "worktree": t._worktree})
            except Exception:
                pass
        return {
            "pending_tool_call": _pending_tool_call(messages),
            "running": running,
            "pending_approval": self._approval_view(messages),
            "last_error": self._last_error,
            "errors": [e for e in self._errors if not e.get("resolved")],
            "resume": self._resume_view(messages),
        }

    def _approval_view(self, messages: list[dict] | None = None) -> dict | None:
        """The command waiting for a human decision, from the structured event.

        Falls back to reading the transcript only when the digest is empty --
        the log's ``approval`` event is authoritative because it does not depend
        on the wording of the tool's own prompt.
        """
        if self._last_approval:
            return self._last_approval
        return _pending_approval(messages if messages is not None
                                 else (getattr(self.agent, "messages", []) or []))

    def _resume_view(self, messages: list[dict] | None = None) -> dict | None:
        """What a restored agent should continue, and how (v2 §11.3).

        ``next`` is deliberately a *structured* next step rather than prose: the
        whole point is that recovery can branch on it ("re-run this call" vs
        "ask the user") instead of asking a model to guess from a timestamp.
        """
        messages = messages if messages is not None else (getattr(self.agent, "messages", []) or [])
        pending = _pending_tool_call(messages)
        approval = self._approval_view(messages)
        text = strip_continue(self._last_user)

        if approval:
            kind = "approval"
            nxt = {"tool": approval.get("tool") or "bash",
                   "args": {"command": approval.get("command", "")},
                   "why": "上一次在等用户批准这条命令"}
        elif pending and pending.get("tool") in REVIEW_TOOLS:
            kind, nxt = "review", pending
        elif pending:
            kind, nxt = "tool", pending
        elif self._explicit_next:
            tool = str(self._explicit_next.get("tool", ""))
            kind = "review" if tool in REVIEW_TOOLS else "task"
            nxt = self._explicit_next
        elif self._last_error and not self._last_error.get("resolved"):
            kind, nxt = "blocked", None
        else:
            kind, nxt = "task", None

        if not text and not nxt:
            return None
        # how many times *this* task has been resumed -- resets when the task
        # changes, because five resumes of five different tasks is not a loop
        generation = self._generation if (text and text == self._resume_text) else 0
        return {"kind": kind if kind in RESUME_KINDS else "task",
                "text": text, "next": nxt,
                "generation": generation, "source_cp": self._head_id()}

    def _env_view(self) -> dict:
        """Environment fingerprint only -- no file contents, ever."""
        import os
        root = _git(["rev-parse", "--show-toplevel"])
        return {
            "cwd": os.getcwd(),
            "is_git": bool(root),
            "git_head": _git(["rev-parse", "--short", "HEAD"]),
            "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty": sorted(_dirty_files()),
            "worktrees": sorted(
                p.name for p in (Path(".worktrees").iterdir()
                                 if Path(".worktrees").exists() else [])),
        }

    def env_diff(self, cp_env: dict) -> list[str]:
        """Human-readable drift between a snapshot's environment and right now.

        Reported, never acted on: rolling files back is git's job.
        """
        cur = self._env_view()
        changes: list[str] = []
        if cp_env.get("cwd") != cur.get("cwd"):
            changes.append(f"工作目录变了：{cp_env.get('cwd')} → {cur.get('cwd')}")
        if cp_env.get("branch") != cur.get("branch"):
            changes.append(f"分支变了：{cp_env.get('branch') or '-'} → {cur.get('branch') or '-'}")
        if cp_env.get("git_head") != cur.get("git_head"):
            changes.append(f"HEAD 变了：{cp_env.get('git_head') or '-'} → {cur.get('git_head') or '-'}")
        added = sorted(set(cur.get("dirty") or []) - set(cp_env.get("dirty") or []))
        if added:
            shown = ", ".join(added[:5]) + ("…" if len(added) > 5 else "")
            changes.append(f"快照之后有 {len(added)} 个文件被改动：{shown}")
        gone = sorted(set(cp_env.get("worktrees") or []) - set(cur.get("worktrees") or []))
        if gone:
            changes.append(f"worktree 已消失：{', '.join(gone)}")
        return changes

    def task_drift(self, cp_tasks: list[dict]) -> list[str]:
        """Where a snapshot's frozen task view and ``.TASK/`` now disagree.

        The view is frozen on purpose (§3: ``.TASK/`` is the source of truth and
        restore must not roll it back), so the two *will* diverge -- a task can
        finish while the process is dead. Diverging *silently* is the problem: a
        restored agent would go on "continuing" work that is already done, while
        the prelude says nothing about it. Reported, never acted on -- the same
        rule as the environment diff.
        """
        live = {t.get("task_id"): t.get("state") for t in self._task_view()}
        changes = []
        for t in cp_tasks:
            tid, was = t.get("task_id"), t.get("state")
            if not tid:
                continue
            now = live.get(tid)
            if now is None:
                changes.append(f"任务 {tid}：快照里是 {was}，现在已不在未完成列表里"
                               "（多半已完成或归档）")
            elif now != was:
                changes.append(f"任务 {tid}：快照里是 {was}，磁盘上是 {now}")
        return changes

    # -- checkpoint files ------------------------------------------------------

    def _checkpoint_files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(self.dir.glob("cp_*.json"))

    def _next_id(self) -> str:
        """``cp_`` + fixed-width sequence, so lexical order is chronological."""
        used = 0
        for p in self._checkpoint_files():
            try:
                used = max(used, int(p.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        for meta in self._read_index():
            try:
                used = max(used, int(str(meta.get("id", "")).split("_")[1]))
            except (IndexError, ValueError):
                continue
        return f"cp_{used + 1:04d}"

    def _write_atomic(self, path: Path, payload: dict) -> None:
        """tmp + replace, the same convention as ``.TASK/`` and ``.Mailbox/``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _append_index(self, meta: dict) -> None:
        """Append to the derived index (never authoritative, rebuildable)."""
        try:
            with self._index_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
                f.flush()
        except OSError:
            pass

    def _read_index(self) -> list[dict]:
        p = self._index_path()
        if not p.exists():
            return []
        out = []
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("id"):
                    out.append(obj)
        except OSError:
            return []
        return out

    def rebuild_index(self) -> int:
        """Rebuild index.jsonl from the checkpoint files themselves."""
        metas = []
        for p in self._checkpoint_files():
            try:
                meta = json.loads(p.read_text(encoding="utf-8")).get("meta")
            except (json.JSONDecodeError, OSError, AttributeError):
                continue
            if isinstance(meta, dict):
                metas.append(meta)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._index_path().write_text(
                "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in metas),
                encoding="utf-8")
        except OSError:
            pass
        return len(metas)

    def _set_head(self, cp_id: str) -> None:
        try:
            self._write_atomic(self._head_path(),
                               {"head": cp_id, "updated_at": _now()})
        except OSError:
            pass

    def head_id(self) -> str:
        return self._head_id()

    def _head_id(self) -> str:
        p = self._head_path()
        if not p.exists():
            return ""
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("head", "")
        except (json.JSONDecodeError, OSError):
            return ""

    # -- listing / loading -----------------------------------------------------

    def count(self) -> int:
        return len(self._checkpoint_files())

    def list_checkpoints(self) -> list[dict]:
        """Snapshot metadata, oldest first. Uses (and repairs) the derived index."""
        files = self._checkpoint_files()
        metas = self._read_index()
        if len(metas) != len(files):
            self.rebuild_index()
            metas = self._read_index()
        return metas

    def load(self, cp_id: str) -> dict | None:
        p = self.dir / f"{cp_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def find(self, cp_id: str) -> dict | None:
        """Load a snapshot from THIS session, or None."""
        return self.load(cp_id)

    def adopt(self, cp_id: str) -> bool:
        """Repoint at whichever session dir holds ``cp_id``. Returns whether it did.

        Checkpoints outlive the process that wrote them, and a restart opens the
        newest session dir -- which may not be the one holding the checkpoint the
        user named. Re-opening that dir's log also means the timeline continues
        where it left off rather than forking a new one.
        """
        if (self.dir / f"{cp_id}.json").exists():
            return True
        if not self.base_dir.exists():
            return False
        for d in sorted(self.base_dir.iterdir()):
            if d.is_dir() and (d / f"{cp_id}.json").exists():
                self.session_id = d.name
                self.log = EventLog(d / "events.jsonl")
                self._last_fingerprint = ""
                # a different session's log means a different recent past
                self._last_user, self._last_error, self._last_approval = "", None, None
                self._errors, self._explicit_next, self._generation = [], None, 0
                self._resume_text, self._pending_resume = "", None
                self._last_summary, self._last_compressed_seq = "", 0
                self._files_seen = set()
                self._rebuild_digest()
                return True
        return False

    def tail_events(self, since_seq: int, limit: int = 10) -> list[Event]:
        events = [e for e in self.log.read(from_seq=since_seq) if e.type != "note"]
        return events[-limit:]

    # -- restore ---------------------------------------------------------------

    def restore(self, cp_id: str, apply=None) -> Restored | None:
        """Pull the agent back to a checkpoint. ``apply`` mutates the agent.

        The order matters and is enforced *here* rather than trusted to the call
        site: the rewind snapshot is taken only **after** the restored state is
        in place. Snapshotting first (as v1 did) recorded the state the user was
        leaving -- so the new head described the wrong world, and re-restoring it
        undid the restore (v2 §11.4).

        Files are NOT rolled back -- the environment diff is reported so the user
        can decide (git's job).
        """
        cp = self.load(cp_id)
        if cp is None:
            return None
        state = cp.get("state", {})
        agent_state = state.get("agent", {})
        messages = repair_chain(agent_state.get("messages", []) or [])
        todos = agent_state.get("todos", []) or []

        # Re-decide "is this failure still live?" against the log as it stands
        # now. The snapshot's verdict describes the past; the events written
        # after it are newer evidence, and the seconds before a crash may
        # already have fixed the problem -- that is the whole anti-re-fix rule.
        execu = dict(state.get("execution", {}) or {})
        live_err = self._recheck_error(execu.get("last_error"))
        execu["errors"] = [e for e in (execu.get("errors") or [])
                           if self._recheck_error(e)]
        execu["last_error"] = live_err or (execu["errors"][0] if execu["errors"] else None)

        resume = dict(execu.get("resume") or {})
        self._arm_resume(resume)

        changes = self.env_diff(state.get("env", {}) or {})
        drift = self.task_drift(agent_state.get("tasks", []) or [])
        # Render from the *corrected* execution block, not the file: the whole
        # point of the re-check above is that the handoff must not report a
        # failure the log has since retired (v2 §11.2d). The snapshot keeps the
        # historical verdict, which is what `/checkpoint show` should print.
        handoff = self.render_handoff({**cp, "state": {**state, "execution": execu}},
                                      changes, drift=drift)
        self.note_handoff(handoff)

        # The rewind is logged but must not snapshot on its own: the snapshot
        # belongs *after* ``apply`` (below), and letting the event trigger one
        # would put a stale state back at the head. History is still never
        # rewritten -- the post-apply snapshot is a new cp, not an edit.
        self._log_only("rewind", actor="user", name=cp_id,
                       data={"restored_from": cp_id, "seq": cp.get("meta", {}).get("seq", 0)})

        restored = Restored(checkpoint_id=cp_id, messages=messages, todos=todos,
                            handoff=handoff, env_changes=changes, resume=resume,
                            last_error=execu.get("last_error"), drift=drift)
        if apply is not None:
            apply(restored)
            # now, and only now, a snapshot describes where the agent actually is
            self.snapshot(trigger="rewind", force=True, actor="user",
                          label=f"恢复到 {cp_id}", reason=f"restore {cp_id}")
        return restored

    def _error_age(self, err: dict | None) -> int:
        """How many events have been logged since this failure. 0 = unknown."""
        seq = int((err or {}).get("seq", 0) or 0)
        if not seq:
            return 0
        try:
            return max(0, int(getattr(self.log, "seq", 0) or 0) - seq)
        except (TypeError, ValueError):
            return 0

    def _recheck_error(self, err: dict | None) -> dict | None:
        """The failure, or None if a later success in the log already retired it.

        Keeps the "don't fix it twice" promise honest: it is not enough to store
        a ``resolved`` flag, because the flag was computed when the snapshot was
        taken and the world has moved since.
        """
        if not err or err.get("resolved"):
            return None
        task = err.get("task") or {}
        seq = int(err.get("seq", 0) or 0)
        if not seq:
            return err
        try:
            later = self.log.read(from_seq=seq + 1)
        except Exception:
            return err
        for e in later:
            if e.type not in ("tool_done", "approval") or e.status != "ok":
                continue
            if (e.tool or e.name) == task.get("tool") and \
                    self._task_of(e)["target"] == task.get("target"):
                return None
        return err

    def _arm_resume(self, resume: dict) -> None:
        """Arm the continuation prefix and advance the generation counter."""
        text = strip_continue(resume.get("text", ""))
        if not text:
            self._pending_resume = None
            return
        # same task again -> one more generation; a different task resets to 1
        self._generation = self._generation + 1 if text == self._resume_text else 1
        self._resume_text = text
        self._pending_resume = {"text": text, "generation": self._generation}

    def render_handoff(self, cp: dict, env_changes: list[str] | None = None,
                       drift: list[str] | None = None) -> str:
        """The deterministic handoff note (never an LLM-generated summary).

        Reads as a *handoff*, not a status report: it leads with why the work
        stopped and what to pick up, and ends by telling the agent to re-orient
        first, because a restored agent must not assume it already knows the
        current state of the world.
        """
        meta = cp.get("meta", {})
        state = cp.get("state", {})
        agent_state = state.get("agent", {})
        ctx = agent_state.get("context", {})
        execu = state.get("execution", {})
        cp_env = state.get("env", {})

        lines = [f"[断点恢复] {meta.get('id')} · {meta.get('created_at', '')} · "
                 f"触发：{meta.get('label') or meta.get('trigger', '')}"]

        err = execu.get("last_error") or {}
        resume = execu.get("resume") or {}
        generation = int(resume.get("generation", 0) or 0)

        # An unresolved failure that is hundreds of events old is almost never
        # why *this* restore happened, and repeating it every time would turn
        # "don't fix it twice" into "keep being reminded of something nobody
        # cares about any more" (§11.2: expiry, not deletion).
        expired = self._error_age(err) > ERROR_EXPIRY_EVENTS

        # Lead with the failure: "why did I stop" is the question a restored
        # agent cannot answer from the transcript alone (v2 §11.2).
        if err and expired:
            task = err.get("task") or {}
            lines.append(f"■ 历史失败（已过期，{self._error_age(err)} 个事件之前，"
                         f"未必相关）：{task.get('tool') or '?'}"
                         f"@{task.get('target') or task.get('command') or '-'}"
                         f"（seq={err.get('seq')}）")
            err = {}
        if err:
            task = err.get("task") or {}
            reason = err.get("reason") or {}
            what = task.get("target") or task.get("command") or ""
            lines.append("■ 上次为什么停下")
            lines.append(f"  {task.get('tool') or '?'}" + (f" → {what}" if what else ""))
            repeat = int(err.get("repeat", 1) or 1)
            lines.append(f"  原因[{reason.get('kind', 'unknown')}]：{reason.get('text', '')}"
                         + (f"（同一处第 {repeat} 次）" if repeat > 1 else ""))
            if task.get("intent"):
                lines.append(f"  当时在做：{task['intent']}")
            if reason.get("kind") == "unknown" and err.get("raw"):
                lines.append(f"  原文：{_brief(err['raw'], 200)}")
            if int(err.get("seq", 0) or 0):
                lines.append(f"  完整输出：events.jsonl 的 seq={err['seq']}"
                             "（用 read_file / grep 直接读，不要猜）")
            # by seq, not identity: after the snapshot round-trips through JSON
            # the same failure is two equal dicts, and `is` would list it twice
            stale = [e for e in (execu.get("errors") or [])
                     if e.get("seq") != err.get("seq")
                     and self._error_age(e) <= ERROR_EXPIRY_EVENTS]
            if stale:
                others = ", ".join(f"{e.get('task', {}).get('tool')}"
                                   f"@{e.get('task', {}).get('target') or '-'}"
                                   f"(seq={e.get('seq')})" for e in stale[:3])
                lines.append(f"  另有未解决的失败：{others}")

        if resume.get("next") or resume.get("text"):
            lines.append("■ 待续")
            if resume.get("text"):
                suffix = f"（同一任务第 {generation} 次恢复）" if generation > 1 else ""
                lines.append(f"  原任务：{_brief(resume['text'], 160)}{suffix}")
            nxt = resume.get("next") or {}
            if nxt.get("tool"):
                args = json.dumps(nxt.get("args") or {}, ensure_ascii=False)
                lines.append(f"  下一步：{nxt['tool']}({_brief(args, 160)})")
                # `why` is what the *system* inferred (an unanswered approval);
                # `reason` is what the model wrote at its own checkpoint. Both
                # answer "why this step", and the model's own words win.
                why = nxt.get("why") or nxt.get("reason")
                if why:
                    lines.append(f"          {_brief(str(why), 200)}")
            if resume.get("kind") == "review":
                lines.append("  这是 review 任务：直接执行上面那条，不要重新派一个队友")
            elif resume.get("kind") == "approval":
                lines.append("  这条还在等人批准：先问用户，不要自己重跑")
            elif resume.get("kind") == "blocked":
                lines.append("  先解决上面那个失败，再继续原任务")

        # Same spot, same failure, over and over: the expensive loop is the
        # retrying, not the restoring, so say so explicitly.
        if generation >= 3 or int(err.get("repeat", 1) or 1) >= 2:
            lines.append("  ⚠️ 这一处已经反复恢复/失败：换一个做法，或停下来问用户；"
                         "不要再原样重跑同一条命令")

        focus = ctx.get("focus") or {}
        todos = agent_state.get("todos", []) or []
        if focus.get("task") or focus.get("todo") or todos:
            lines.append("■ 当前焦点")
            for t in agent_state.get("tasks", []) or []:
                if t.get("task_id") == focus.get("task"):
                    line = f"  task {t['task_id']}「{t.get('description', '')}」{t.get('state')}"
                    if t.get("note"):
                        line += f"\n      note: {t['note']}"
                    lines.append(line)
            for t in todos:
                if t.get("todo_id") == focus.get("todo"):
                    lines.append(f"  todo {t['todo_id']}「{t.get('title', '')}」{t.get('status')}")

        open_todos = [t for t in todos if t.get("status") != "completed"]
        if open_todos:
            lines.append("■ 未完成 todo")
            for t in open_todos[:8]:
                lines.append(f"  [{t.get('todo_id')}] {t.get('status')}  {t.get('title', '')}")

        tasks = agent_state.get("tasks", []) or []
        if tasks:
            lines.append("■ 未完成任务（详情以 .TASK/ 为准）")
            for t in tasks[:8]:
                line = f"  {t.get('task_id')} {t.get('state')}  {t.get('description', '')}"
                if t.get("blockedBy"):
                    line += f"  依赖: {t['blockedBy']}"
                lines.append(line)

        # The frozen task view and .TASK/ *will* diverge (restore must not roll
        # the source of truth back), so say where -- silence here is how an agent
        # ends up "continuing" a task that finished while it was dead.
        drift_lines = drift if drift is not None else self.task_drift(tasks)
        if drift_lines:
            lines.append("■ 任务视图已过期（以 .TASK/ 为准）")
            for d in drift_lines[:5]:
                lines.append(f"  ⚠️ {d}")

        files = ctx.get("files") or []
        if files:
            lines.append("■ 本会话改过的文件（只是指针，内容请自己读）")
            lines.append("  " + ", ".join(files[:12]))

        running = execu.get("running") or []
        if running:
            lines.append("■ 队友")
            for r in running:
                line = f"  {r.get('name')} {r.get('status')}"
                if r.get("branch"):
                    line += f" ／ 分支 {r['branch']}"
                lines.append(line)

        if ctx.get("compressed_at"):
            lines.append(f"■ 原始过程\n  messages 在 seq={ctx['compressed_at']} 处被压缩过；"
                         "以下是从事件日志取回的最近动作：")
            for e in self.tail_events(int(ctx["compressed_at"]), limit=10):
                bits = [str(e.seq), e.type]
                if e.tool:
                    bits.append(e.tool)
                if e.files:
                    bits.append(", ".join(str(f) for f in e.files[:3]))
                if e.status:
                    bits.append(e.status)
                # ``input`` carries the call's arguments (the command, the path);
                # the old code read ``data["command"]``, which nothing ever wrote,
                # so this column was always blank.
                if e.input:
                    bits.append(_brief(e.input, 60))
                lines.append("    " + "  ".join(bits))

        lines.append("■ 环境")
        lines.append(f"  cwd={cp_env.get('cwd', '?')} branch={cp_env.get('branch') or '-'} "
                     f"HEAD={cp_env.get('git_head') or '-'}")
        changes = env_changes if env_changes is not None else self.env_diff(cp_env)
        if changes:
            for c in changes:
                lines.append(f"  ⚠️ {c}")
        else:
            lines.append("  环境没有变化")

        # ...unless 待续 already named it. Both blocks are fed by the same pending
        # call, so printing both says one thing twice -- and after v2 they could
        # even *disagree*, because each carried its own "why". One owner for the
        # sentence (``_why_unanswered``) and one place it is printed.
        approval = execu.get("pending_approval") or {}
        pending = execu.get("pending_tool_call") or {}
        covered = bool((resume.get("next") or {}).get("tool"))
        if not covered and (approval.get("command") or approval.get("reason")):
            lines.append(f"■ 上次中断\n  等待人工批准的命令：`{approval.get('command', '')}`"
                         + (f"（原因：{approval['reason']}）" if approval.get("reason") else "")
                         + " ← 先问用户，不要自己重跑")
        elif not covered and pending.get("id"):
            args = json.dumps(pending.get("args") or {}, ensure_ascii=False)
            lines.append(f"■ 上次中断\n  {pending.get('tool') or '?'}({_brief(args, 200)}) "
                         "没执行完（已按 interrupt 补记为未答复）"
                         f"{'：' + pending['why'] if pending.get('why') else ''}")

        lines.append("■ 请先做（不要直接改代码）")
        lines.append("  1) git diff 核对环境差异  2) 重读上面列出的文件"
                     "  3) 想接着做就回复「继续」，会把原任务一起带回来")
        return "\n".join(lines)

    def render_summary(self, max_show: int = 3) -> str:
        """Short deterministic block for the TUI sidebar / a nudge."""
        metas = self.list_checkpoints()
        if not metas:
            return ""
        head = self._head_id()
        lines = [f"# Checkpoint（{len(metas)} 个断点，当前 {head or '-'}）"]
        for m in metas[-max_show:]:
            mark = "←" if m.get("id") == head else "  "
            lines.append(f"{mark} {m.get('id')}  {m.get('created_at', '')}  {m.get('label', '')}")
        return "\n".join(lines)

    # -- compaction ------------------------------------------------------------

    def compact(self, keep: int | None = None) -> str:
        """Merge old snapshots into one per contiguous run (cp_1..cp_100 -> cp_11).

        Keeps the first snapshot, the most recent ``keep``, and every milestone.
        The merged snapshot keeps the newest state of its run but replaces the
        messages with an LLM summary (falling back to key-info extraction), and
        its ``parent_id`` re-attaches to the run's first parent so the chain
        stays connected. Originals move to ``archive/`` -- nothing is deleted.

        The event log is cut at the last absorbed checkpoint's seq: a merged
        snapshot now stands in for those events, so the live log stays bounded.
        Older lines move to ``archive/`` behind a ``note`` marker rather than
        being dropped, so the trail is still there -- just one file over.
        """
        keep = self.keep if keep is None else keep
        with self._lock:
            metas = self._ordered_metas()
            if len(metas) <= keep:
                return "(compact) 断点数量未超过阈值，无需压缩"
            protected = set()
            if metas:
                protected.add(metas[0]["id"])
            for m in metas[-keep:]:
                protected.add(m["id"])
            for m in metas:
                if m.get("trigger") in MILESTONE_TRIGGERS:
                    protected.add(m["id"])

            runs: list[list[dict]] = []
            current: list[dict] = []
            for m in metas:
                if m["id"] in protected:
                    if current:
                        runs.append(current)
                        current = []
                else:
                    current.append(m)
            if current:
                runs.append(current)
            if not runs:
                return "(compact) 没有可合并的断点（都是受保护的里程碑）"

            merged_count = 0
            new_ids = []
            merged_upto = 0
            for run in runs:
                # a run of one is just a rename: no state is saved, and it would
                # only cost an id. Only merge when something actually collapses.
                if len(run) < 2:
                    continue
                new_id = self._merge_run(run)
                if new_id:
                    new_ids.append(new_id)
                    merged_count += len(run)
                    merged_upto = max(merged_upto, run[-1].get("seq", 0))

            if not new_ids:
                return "(compact) 没有可合并的断点（受保护的里程碑之间没有连续段）"

            archived = self.log.archive(merged_upto, self.archive_dir)
            self.rebuild_index()
            note = f"，事件日志已归档到 archive/{archived.name}" if archived else ""
            return (f"(compact) {merged_count} 个断点 → {', '.join(new_ids)}"
                    f"（原件在 archive/）{note}")

    def _ordered_metas(self) -> list[dict]:
        files = self._checkpoint_files()
        metas = []
        for p in files:
            try:
                meta = json.loads(p.read_text(encoding="utf-8")).get("meta")
            except (json.JSONDecodeError, OSError, AttributeError):
                continue
            if isinstance(meta, dict):
                metas.append(meta)
        return metas

    def _merge_run(self, run: list[dict]) -> str | None:
        """Merge a contiguous run of snapshots into one, reusing the run's first id.

        Reusing ``run[0]["id"]`` is deliberate: ids are zero-padded so that
        lexical order equals chronological order, and a fresh id would sort to
        the end -- dropping the merged snapshot out of its position in the
        timeline. The absorbed ids are recorded in ``replaces``.
        """
        first, last = run[0], run[-1]
        new_id = first["id"]
        payload = self.load(last["id"])
        if payload is None:
            return None

        state = payload.get("state", {})
        messages = (state.get("agent", {}) or {}).get("messages", []) or []
        summary = self._summarize(messages)
        state.setdefault("agent", {})["messages"] = [
            {"role": "user",
             "content": f"[Context compressed - conversation summary]\n{summary}"},
            {"role": "assistant",
             "content": "Got it, I have the context from our earlier conversation."},
        ]

        # An unresolved failure must survive the merge. Taking only the last
        # member's state means a failure recorded earlier in the run -- exactly
        # the "why am I stuck" the handoff exists to answer -- would be silently
        # compacted away along with the snapshots that carried it.
        merged_errors: dict[tuple, dict] = {}
        for m in run:
            payload_m = payload if m["id"] == last["id"] else self.load(m["id"])
            for err in (((payload_m or {}).get("state", {}) or {})
                        .get("execution", {}) or {}).get("errors") or []:
                if err.get("resolved"):
                    continue
                key = _error_key(err)
                if key not in merged_errors or \
                        int(err.get("seq", 0) or 0) > int(merged_errors[key].get("seq", 0) or 0):
                    merged_errors[key] = err
        if merged_errors:
            kept = sorted(merged_errors.values(), key=lambda e: int(e.get("seq", 0) or 0))
            execu = state.setdefault("execution", {})
            execu["errors"] = kept
            execu.setdefault("last_error", None)
            if not execu.get("last_error"):
                execu["last_error"] = kept[-1]

        meta = {
            "id": new_id,
            "parent_id": first.get("parent_id", ""),
            "created_at": last.get("created_at", _now()),
            "trigger": "compacted",
            "label": f"合并 {first['id']}..{last['id']}（{len(run)} 个断点）",
            "seq": last.get("seq", 0),
            "reason": "compaction",
            "compacted": True,
            "replaces": [m["id"] for m in run],
        }

        # archive the originals FIRST (never delete) -- writing before moving
        # would let the move below cart off the merged file itself.
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            for m in run:
                src = self.dir / f"{m['id']}.json"
                if src.exists():
                    shutil.move(str(src), str(self.archive_dir / src.name))
        except OSError:
            return None
        try:
            self._write_atomic(self.dir / f"{new_id}.json",
                               {"meta": meta, "state": state})
        except OSError:
            return None
        return new_id

    def _summarize(self, messages: list[dict]) -> str:
        """Reuse ContextManager's summarizer (with its no-LLM fallback)."""
        from .context import ContextManager
        if not messages:
            return "(empty)"
        try:
            return ContextManager()._get_summary(messages, self._llm or
                                                 getattr(self.agent, "llm", None))
        except Exception:
            return ContextManager._extract_key_info(messages)




# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _default_label(trigger: str) -> str:
    return {
        "turn_end": "回合结束",
        "compress": "上下文压缩前",
        "approval": "等待人工批准",
        "interrupt": "被中断",
        "stage_done": "阶段性完成",
        "task_done": "任务完成",
        "rewind": "回退到此断点",
        "manual": "手动打点",
        "long": "长耗时调用前",
        "teammate": "队友状态变化",
        "tool": "工具执行后",
        "state": "状态变化",
        "compacted": "压缩合并",
    }.get(trigger, trigger)


def _fingerprint(state: dict) -> str:
    """Hash of the whole state -- 'did anything change since the last snapshot?'"""
    try:
        blob = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(state)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _error_key(err: dict) -> tuple[str, str, str]:
    """Identity of a failure for repeat-counting, resolving and merging."""
    task = err.get("task") or {}
    return (str(task.get("tool", "")), str(task.get("target", "")),
            str((err.get("reason") or {}).get("kind", "")))


def _pending_tool_call(messages: list[dict]) -> dict | None:
    """The last tool_call that never got a reply -- and *what it was*.

    The id alone is not enough (v2 §11.3): knowing that a call was interrupted
    without knowing which tool with which arguments is exactly the gap that makes
    a restored agent unable to continue. The name and arguments are already in
    the assistant message; the old version simply never read them.
    """
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    pending: dict | None = None
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not tid or tid in answered:
                continue
            fn = tc.get("function") if isinstance(tc, dict) else tc
            name = ""
            args = {}
            if isinstance(fn, dict):
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
            else:
                name = getattr(fn, "name", "")
                raw = getattr(fn, "arguments", "") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    args = {}
            pending = {"id": str(tid), "tool": str(name), "args": args,
                       "why": _why_unanswered(str(name), args)}
    return pending


def _why_unanswered(tool: str, args: dict) -> str:
    """Why a call has no reply -- and the *first* question is whether it started.

    Two very different stories produce the same shape in the log. An ordinary
    tool call unanswered means the agent was interrupted between asking and
    getting an answer. A *long* call unanswered means the process very likely
    died while it was running -- and re-running an install or a build blind is
    the one move that can make things worse (⑤). Same shape, different first
    move, so the reason lives here where both renderers read it from (one
    sentence, one owner -- the handoff used to print two).
    """
    if looks_long(tool, args):
        return ("上一次是长耗时调用且没有答复：进程很可能是在它执行到一半时被杀掉的。"
                "先确认它跑到哪里、有没有留下副作用，再决定是接着跑还是重跑")
    return "上一次调用被中断，没有执行完"


def _pending_approval(messages: list[dict]) -> dict | None:
    """Fallback: the high-risk command awaiting approval, read from the transcript.

    Only used when the digest has nothing (e.g. a snapshot written by an older
    version). The live path reads the structured ``approval`` event instead of
    pattern-matching the tool's prose, which used to break silently whenever the
    confirm wording changed (v2 §11.3).
    """
    from .tools.bash import NEEDS_CONFIRM
    for m in reversed(messages[-6:]):
        content = m.get("content") or ""
        if m.get("role") == "tool" and NEEDS_CONFIRM in content:
            command = ""
            for line in content.splitlines():
                if line.startswith("Command:"):
                    command = line[len("Command:"):].strip()
                    break
            return {"tool": "bash", "command": command,
                    "reason": "", "seq": 0, "at": "",
                    "status": "pending"}
    return None


def repair_chain(messages: list[dict]) -> list[dict]:
    """Backfill ``[interrupted]`` for tool calls that never got a reply.

    A snapshot can land exactly where a crash did -- an assistant message with
    tool_calls and no matching tool replies -- which OpenAI-compatible APIs
    reject. Same semantics as ``Agent._answer_pending_tool_calls``.
    """
    out = copy.deepcopy(messages or [])
    answered = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for m in list(out):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid and tid not in answered:
                out.append({"role": "tool", "tool_call_id": tid,
                            "content": "[interrupted]"})
                answered.add(tid)
    return out


def _git(args: list[str]) -> str:
    """Best-effort git query; empty string when not a repo or git is missing."""
    try:
        cp = subprocess.run(["git", *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=5,
                            check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def _dirty_files() -> set[str]:
    out = _git(["status", "--porcelain"])
    files = set()
    for line in out.splitlines():
        name = line[3:].strip().strip('"')
        if " -> " in name:              # renames: keep the destination
            name = name.split(" -> ")[-1].strip()
        if name:
            files.add(name)
    return files
