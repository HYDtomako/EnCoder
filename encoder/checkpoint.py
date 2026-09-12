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

#: Tools whose effects re-running the agent cannot undo. A snapshot after one of
#: these is worth its cost; after ``read_file`` / ``grep`` it is pure waste.
IRREVERSIBLE_TOOLS = frozenset({
    "write_file", "edit_file", "bash",
    "spawn_teammate", "review_teammate", "release_teammate", "integrate_results",
    "create_schedule", "delete_schedule",
})

#: Triggers that always snapshot, bypassing the throttle.
HARD_TRIGGERS = frozenset({
    "turn_end", "compress", "approval", "interrupt", "stage_done",
    "task_done", "rewind", "manual", "teammate",
})

#: Triggers that snapshot only once the throttle above has passed.
SOFT_TRIGGERS = frozenset({"tool", "state"})

#: Triggers compaction must never merge away -- each marks a point a human or the
#: model deliberately wanted to be able to come back to.
MILESTONE_TRIGGERS = frozenset({
    "turn_end", "compress", "approval", "interrupt", "stage_done",
    "task_done", "rewind", "manual",
})

#: Triggers that also skip fingerprint de-duplication. These are *deliberate*
#: boundaries: "I was interrupted here" or "this task just finished" is worth
#: recording even when the derived state happens to look unchanged.
FORCE_TRIGGERS = frozenset({"manual", "interrupt", "task_done", "rewind", "approval"})

#: Event types recognised by ``trigger_for``. Anything else never snapshots.
_EVENT_TYPES = frozenset({
    "session_start", "user_message", "message_added", "tool_done", "todo_changed",
    "task_changed", "teammate_changed", "compress", "approval", "interrupt",
    "turn_end", "manual", "rewind", "note",
})

_OUTPUT_LIMIT = 800         # chars of tool output kept in an event
_INPUT_LIMIT = 500          # chars of tool arguments kept in an event
_TAIL_BYTES = 65536         # how much of events.jsonl to read to find the last seq


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _brief(text, limit: int) -> str:
    """Truncate a value for the event log (events are a trail, not a transcript)."""
    s = "" if text is None else str(text)
    s = s.strip()
    return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} chars)"


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


def trigger_for(event: Event) -> str | None:
    """Map an event type to a trigger, or None when it can never justify a snapshot.

    This is the ONLY mapping between the two vocabularies; keep it exhaustive.
    """
    t = event.type
    if t == "tool_done":
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

    if trigger in SOFT_TRIGGERS:
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
                marker = Event(seq=self._seq, ts=_now(), type="note", actor="system",
                               data={"compact": "events", "kept_from_seq": keep_from_seq,
                                     "archived": dest.name, "lines": len(old_lines)})
                self.path.write_text(
                    "\n".join([json.dumps(marker.to_dict(), ensure_ascii=False)]
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

        path = self.base_dir / self.session_id
        self.log = EventLog(path / "events.jsonl")
        if self.enabled:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False     # unwritable -> degrade to a no-op

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
            event = self.log.emit(event_type, actor=actor, **fields)
            for f in event.files or []:
                self._files_seen.add(str(f))
            for f in (event.data or {}).get("files") or []:
                self._files_seen.add(str(f))
            self._maybe_snapshot(event)
            return event
        except Exception:
            return None            # checkpointing must never break the agent loop

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
        """
        with self._lock:
            self._last_summary = summary or self._last_summary
            self._last_compressed_seq = self.log.seq

    def note_handoff(self, block: str) -> None:
        """Hold a handoff block until the next request's prelude consumes it."""
        self._last_handoff = block or ""

    def take_handoff(self) -> str:
        """Read-and-clear the pending handoff block (prelude channel)."""
        block, self._last_handoff = self._last_handoff, ""
        return block

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
            "pending_tool_call": _pending_tool_call_id(messages),
            "running": running,
            "pending_approval": _pending_approval(messages),
        }

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
                return True
        return False

    def tail_events(self, since_seq: int, limit: int = 10) -> list[Event]:
        events = [e for e in self.log.read(from_seq=since_seq) if e.type != "note"]
        return events[-limit:]

    # -- restore ---------------------------------------------------------------

    def restore(self, cp_id: str) -> Restored | None:
        """Prepare a restore. The caller applies ``messages``/``todos`` to the agent.

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
        changes = self.env_diff(state.get("env", {}) or {})
        handoff = self.render_handoff(cp, changes)
        self.note_handoff(handoff)
        # the rewind itself is an event, and the resulting snapshot is new --
        # history is never rewritten (LangGraph's "never mutate old checkpoints")
        self.record("rewind", actor="user", name=cp_id,
                    data={"restored_from": cp_id, "seq": cp.get("meta", {}).get("seq", 0)})
        return Restored(checkpoint_id=cp_id, messages=messages, todos=todos,
                        handoff=handoff, env_changes=changes)

    def render_handoff(self, cp: dict, env_changes: list[str] | None = None) -> str:
        """The deterministic handoff note (never an LLM-generated summary).

        Reads as a *handoff*, not a status report: it ends by telling the agent
        to re-orient first, because a restored agent must not assume it already
        knows the current state of the world.
        """
        meta = cp.get("meta", {})
        state = cp.get("state", {})
        agent_state = state.get("agent", {})
        ctx = agent_state.get("context", {})
        execu = state.get("execution", {})
        cp_env = state.get("env", {})

        lines = [f"[断点恢复] {meta.get('id')} · {meta.get('created_at', '')} · "
                 f"触发：{meta.get('label') or meta.get('trigger', '')}"]

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
                if e.data.get("command"):
                    bits.append(_brief(e.data["command"], 60))
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

        if execu.get("pending_approval"):
            lines.append(f"■ 上次中断\n  等待人工批准的命令：`{execu['pending_approval']}` "
                         "← 先问用户，不要自己重跑")
        elif execu.get("pending_tool_call"):
            lines.append(f"■ 上次中断\n  工具调用 {execu['pending_tool_call']} 未执行完"
                         "（已按 interrupt 补记为未答复）")

        lines.append("■ 请先做（不要直接改代码）")
        lines.append("  1) git diff 核对环境差异  2) 重读上面列出的文件  3) 然后继续")
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


def _pending_tool_call_id(messages: list[dict]) -> str:
    """The last tool_call that never got a reply -- what a crash leaves behind."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    pending = ""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid and tid not in answered:
                pending = tid
    return pending


def _pending_approval(messages: list[dict]) -> str:
    """The high-risk command the agent is waiting for the user to approve."""
    from .tools.bash import NEEDS_CONFIRM
    for m in reversed(messages[-6:]):
        content = m.get("content") or ""
        if m.get("role") == "tool" and NEEDS_CONFIRM in content:
            for line in content.splitlines():
                if line.startswith("Command:"):
                    return line[len("Command:"):].strip()
            return content.splitlines()[0][:200] if content else ""
    return ""


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
