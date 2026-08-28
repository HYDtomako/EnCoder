"""Persistent task manager + session-scoped todo list (DESIGN_todo_task_v1.md).

Two concepts, kept deliberately separate:

- **Todo**: a lightweight, *session-scoped* checklist the Lead agent maintains for
  the current user request. It is in-memory only, never dispatched, no
  dependencies. Cleared when a new request starts.
- **Task**: a *persisted* work unit stored under `.TASK/<slug>.json` (one task one
  file; slug = a short readable description). A task may depend on other tasks via
  ``blockedBy`` and can only start when all of them are completed. It is dispatched
  to an agent by the Lead and its state machine is fully owned by :class:`TaskManager`.

Layout::

    .TASK/<slug>.json            one active task per file
    .TASK/done/<root>-<ts>.json  archived root-task subgraph (one aggregate file)

State machine (add2.0)::

    pending --(all blockedBy completed)--> in_progress --(agent done)--> completed

Guarantees:

- Every state change is written back to disk synchronously (no caching/batching).
- ``TaskManager`` holds a ``threading.Lock`` guarding multi-file reads (dependency
  checks) and id generation, ready for add3.0's parallel agent_team.
- ``MAX_ATTEMPTS`` caps auto re-dispatch; beyond it the Lead must intervene.
"""

from __future__ import annotations   # lazy annotations: TaskManager.list shadows builtin list

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

STATES: tuple[str, ...] = ("pending", "in_progress", "completed")
PRIORITIES: tuple[str, ...] = ("high", "normal", "low")
MAX_ATTEMPTS = 3  # per-task dispatch attempts; beyond this the Lead intervenes

_DONE_DIRNAME = "done"
_ACTIVE_FIELDS = frozenset(
    ("task_id", "agent", "state", "description", "blockedBy", "created_at",
     "updated_at", "assignee", "result", "attempts", "priority", "last_error", "note")
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _file_slug(description: str, task_id: str) -> str:
    """Short readable filename slug from a task description (not the opaque id).

    CJK + ASCII words are kept, everything else collapses to '-', capped at 40
    chars. Falls back to task_id for an empty/blank description.
    """
    s = re.sub(r"[^A-Za-z0-9一-鿿]+", "-", (description or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > 40:
        s = s[:40].rstrip("-")
    return s or task_id


def _to_task(data: dict) -> "Task":
    """Build a Task from raw json, ignoring unknown keys defensively."""
    return Task(**{k: v for k, v in data.items() if k in _ACTIVE_FIELDS})


# --------------------------------------------------------------------------- #
# Todo - session-scoped checklist
# --------------------------------------------------------------------------- #

@dataclass
class Todo:
    todo_id: str
    title: str
    status: str = "pending"             # pending | in_progress | completed
    task_id: str | None = None          # optional: which task this todo serves
    note: str = ""


class TodoList:
    """The Lead's checklist for the current request. In-memory, not persisted."""

    def __init__(self) -> None:
        self._items: list[Todo] = []
        self._counter = 0

    def create(self, titles: list[str], task_id: str | None = None) -> list[Todo]:
        """Create 1..N todos (all pending), return the created todos."""
        created: list[Todo] = []
        for title in titles:
            title = str(title).strip()
            if not title:
                continue
            self._counter += 1
            todo = Todo(todo_id=f"t{self._counter}", title=title, task_id=task_id)
            self._items.append(todo)
            created.append(todo)
        return created

    def update(self, todo_id: str, *, status: str | None = None,
               note: str | None = None) -> Todo | None:
        """Update one todo. Returns the todo, or None if the id is unknown."""
        for todo in self._items:
            if todo.todo_id == todo_id:
                if status is not None:
                    if status not in STATES:
                        raise ValueError(f"invalid status '{status}', expected {list(STATES)}")
                    todo.status = status
                if note is not None:
                    todo.note = str(note)
                return todo
        return None

    def list(self) -> list[Todo]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._counter = 0


# --------------------------------------------------------------------------- #
# Task - persisted work unit
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    task_id: str = ""
    agent: str = ""                     # agent that should handle it (name, no hardcoded role)
    state: str = "pending"              # pending | in_progress | completed
    description: str = ""
    blockedBy: list[str] = field(default_factory=list)   # task_ids this task depends on
    created_at: str = ""
    updated_at: str = ""
    assignee: str | None = None         # agent that actually ran the last dispatch
    result: str | None = None           # written back when completed
    attempts: int = 0                   # dispatch attempts so far
    priority: str = "normal"            # high | normal | low
    last_error: str | None = None       # last dispatch failure, for the Lead to judge
    note: str = ""


class TaskManager:
    """Owns the whole lifetime and state machine of persisted Tasks.

    Every mutation reads the file, validates, changes, and writes back
    atomically under a lock. Tools and the CLI must go through these methods;
    nothing else may touch ``.TASK/`` directly.
    """

    def __init__(self, base_dir: Path = Path(".TASK")) -> None:
        self.base_dir = Path(base_dir)
        self.done_dir = self.base_dir / _DONE_DIRNAME
        self._lock = threading.Lock()
        self._index: dict[str, Path] | None = None   # task_id -> file, built lazily

    # -- lookup --------------------------------------------------------------

    def _scan(self) -> dict[str, Path]:
        """Rebuild task_id -> file index from active .TASK/*.json (not done/)."""
        idx: dict[str, Path] = {}
        if self.base_dir.exists():
            for p in self.base_dir.glob("*.json"):
                try:
                    tid = json.loads(p.read_text(encoding="utf-8")).get("task_id")
                except (json.JSONDecodeError, OSError):
                    continue
                if tid:
                    idx[tid] = p
        return idx

    def _index_get(self, task_id: str) -> Path | None:
        if self._index is None:
            self._index = self._scan()
        return self._index.get(task_id)

    def _invalidate(self) -> None:
        self._index = None

    def get(self, task_id: str) -> Task | None:
        path = self._index_get(task_id)
        if path is None:
            return None
        try:
            return _to_task(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def list(self) -> list[Task]:
        """All active tasks, sorted by priority (high first) then creation time."""
        order = {"high": 0, "normal": 1, "low": 2}
        with self._lock:
            paths = set(self._scan().values())
        tasks: list[Task] = []
        for p in paths:
            try:
                tasks.append(_to_task(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        tasks.sort(key=lambda t: (order.get(t.priority, 1), t.created_at))
        return tasks

    # -- file path -----------------------------------------------------------

    def _file_path(self, task: Task) -> Path:
        """File for a task: readable slug, disambiguated if the slug is taken."""
        base = _file_slug(task.description, task.task_id)
        path = self.base_dir / f"{base}.json"
        if path.exists():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("task_id") == task.task_id:
                    return path            # this exact task already lives here
            except (json.JSONDecodeError, OSError):
                pass
            path = self.base_dir / f"{base}-{task.task_id[:4]}.json"
        return path

    def path_of(self, task_id: str) -> Path | None:
        """Absolute-ish path of a task file, for display (None if unknown)."""
        return self._index_get(task_id)

    # -- create / update / save ----------------------------------------------

    def create(self, description: str, agent: str = "", blocked_by: list[str] | None = None,
               priority: str = "normal") -> Task:
        """Persist a new pending task and return it."""
        description = str(description).strip()
        if not description:
            raise ValueError("description must not be empty")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {list(PRIORITIES)}")
        blocked_by = list(blocked_by or [])
        for dep in blocked_by:
            if self.get(dep) is None:
                raise ValueError(f"blocked_by references unknown task '{dep}'")

        with self._lock:
            task_id = self._new_id()
            task = Task(
                task_id=task_id,
                agent=str(agent or ""),
                description=description,
                blockedBy=blocked_by,
                priority=priority,
                created_at=_now(),
            )
            self.save(task)
        return task

    def update(self, task_id: str, *, state: str | None = None,
               priority: str | None = None, note: str | None = None) -> Task:
        """Manual Lead fallback; state/priority changes still go through validation."""
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"no such task '{task_id}'")
        if state is not None:
            if state not in STATES:
                raise ValueError(f"invalid state '{state}', expected {list(STATES)}")
            task.state = state
            if state == "completed" and task.result is None:
                task.result = task.note or "(completed manually)"
        if priority is not None:
            if priority not in PRIORITIES:
                raise ValueError(f"invalid priority '{priority}', expected {list(PRIORITIES)}")
            task.priority = priority
        if note is not None:
            task.note = str(note)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        """Atomic write of one task file (tmp + replace)."""
        task.updated_at = _now()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._index_get(task.task_id) or self._file_path(task)
        tmp = self.base_dir / f"{path.name}.tmp"
        tmp.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        if self._index is not None:
            self._index[task.task_id] = path

    def clear(self) -> None:
        """Delete every active task file (archive kept). Dangerous; user-confirmed."""
        with self._lock:
            for p in self._scan().values():
                try:
                    p.unlink()
                except OSError:
                    pass
            self._invalidate()

    # -- state machine --------------------------------------------------------

    def _deps_unfinished(self, task: Task) -> list[str]:
        """Dependencies that are missing or not yet completed."""
        return [d for d in task.blockedBy
                if (self.get(d) is None or self.get(d).state != "completed")]

    def can_start(self, task_id: str) -> tuple[bool, list[str]]:
        """(True, []) iff the task is pending and every blockedBy is completed."""
        task = self.get(task_id)
        if task is None:
            return False, [task_id]
        if task.state != "pending":
            return False, []
        blocked = self._deps_unfinished(task)
        return (not blocked), blocked

    def mark_in_progress(self, task_id: str, assignee: str | None = None) -> Task:
        """pending -> in_progress (first dispatch) or retry of a failed in_progress.

        Refuses when dependencies are unfinished, the task is already running
        (no last_error), or the state does not allow starting.
        """
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"no such task '{task_id}'")
        blocked = self._deps_unfinished(task)
        if blocked:
            raise RuntimeError(f"cannot start: dependencies not completed: {blocked}")
        if task.state not in ("pending", "in_progress"):
            raise RuntimeError(f"cannot start task from state '{task.state}'")
        if task.state == "in_progress" and task.last_error is None:
            raise RuntimeError("task already in progress")
        task.state = "in_progress"
        task.assignee = assignee
        task.attempts += 1
        task.last_error = None
        self.save(task)
        return task

    def mark_completed(self, task_id: str, result: str | None = None) -> Task:
        """in_progress -> completed, writing back the agent's result."""
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"no such task '{task_id}'")
        if task.state != "in_progress":
            raise RuntimeError(f"cannot mark completed from state '{task.state}'")
        task.state = "completed"
        task.result = result
        task.last_error = None
        self.save(task)
        return task

    def can_retry(self, task_id: str) -> bool:
        """True when a failed dispatch may be attempted again."""
        task = self.get(task_id)
        if task is None:
            return False
        return task.last_error is not None and task.attempts < MAX_ATTEMPTS

    def ready_tasks(self) -> list[Task]:
        """Pending tasks whose dependencies are all completed, high priority first."""
        order = {"high": 0, "normal": 1, "low": 2}
        ready = [t for t in self.list()
                 if t.state == "pending" and not self._deps_unfinished(t)]
        ready.sort(key=lambda t: (order.get(t.priority, 1), t.created_at))
        return ready

    # -- proactive work surfacing (xuigai2.0: Lead should judge by itself) ----

    def unfinished(self) -> list[Task]:
        """Active tasks not yet completed (pending / in_progress), for continuation."""
        return [t for t in self.list() if t.state != "completed"]

    def pending_reminder(self, max_show: int = 12) -> str:
        """Deterministic reminder injected at the start of each request.

        Returns an empty string when nothing in .TASK/ is unfinished, so a clean
        task list costs the Lead zero noise. When leftover work exists (across
        sessions, since tasks persist), the block lists it and tells the Lead to
        decide: dispatch / update / archive, or ignore and say so in one line.
        """
        uf = self.unfinished()
        if not uf:
            return ""
        lines = []
        for t in uf[:max_show]:
            line = f"  [{t.task_id}] {t.state:<11} {t.description}"
            if t.priority != "normal":
                line += f"  优先级: {t.priority}"
            if t.blockedBy:
                line += f"  依赖: {t.blockedBy}"
            lines.append(line)
        if len(uf) > max_show:
            lines.append(f"  … 及另外 {len(uf) - max_show} 个未完成任务")
        return (
            f"[⚠️ 持久化任务提醒] .TASK/ 下仍有 {len(uf)} 个未完成任务：\n"
            + "\n".join(lines)
            + "\n请自行判断：继续派发（dispatch_task，依赖完成后才可开始）/ "
              "更新（update_task）/ 完成或归档根任务（archive_tasks）；"
              "若与本次请求无关，忽略并一句话说明即可，不要强行执行。然后处理用户请求。"
        )

    # -- archive ---------------------------------------------------------------

    def archive(self, root_id: str) -> list[Task]:
        """Archive the whole dependency subgraph of a *completed* root task.

        Guarantees the overall task is truly done (root + every transitively
        blocked task completed) before writing one aggregate file under
        ``.TASK/done/`` and removing the active files.
        """
        root = self.get(root_id)
        if root is None:
            raise KeyError(f"no such task '{root_id}'")
        if root.state != "completed":
            raise RuntimeError("root task is not completed yet; cannot archive")

        ids: set[str] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur.task_id in ids:
                continue
            ids.add(cur.task_id)
            for dep in cur.blockedBy:
                d = self.get(dep)
                if d is not None:
                    stack.append(d)

        tasks = [t for i in ids if (t := self.get(i)) is not None]
        unfinished = [t.task_id for t in tasks if t.state != "completed"]
        if unfinished:
            raise RuntimeError(f"cannot archive: subgraph not all completed: {unfinished}")

        with self._lock:
            self.done_dir.mkdir(parents=True, exist_ok=True)
            dest = self.done_dir / f"{_file_slug(root.description, root.task_id)}-{_ts()}.json"
            dest.write_text(
                json.dumps([asdict(t) for t in tasks], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for t in tasks:
                path = self._index_get(t.task_id)
                if path is not None and path.exists():
                    path.unlink()
            self._invalidate()
        return tasks

    def _new_id(self) -> str:
        """Collision-free task_id (uuid short hex), checked under the lock."""
        while True:
            tid = uuid.uuid4().hex[:10]
            if self._index_get(tid) is None:
                return tid
