"""Cron scheduler - daily scheduled tasks for the agent.

A user can ask the agent to do something on a schedule ("every day at 8am,
collect the AI news and summarize it for me").  The task is stored globally
under ~/.corecoder/tasks.json so it survives restarts and only stops when the
user deletes it.

A background daemon thread polls the clock; when a task's HH:MM has arrived
and it hasn't fired today yet, it is enqueued.  The main loop (REPL or
-daemon mode) drains the queue and runs each task as an ordinary user message,
so a triggered task never competes with the agent's current work (add.md).

The thread only touches the clock and the queue/tasks file; the model,
network and agent loop stay on the main thread.
"""

import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TASKS_FILE = Path.home() / ".corecoder" / "tasks.json"
_TASKS_DIR = TASKS_FILE.parent

# "HH:MM", e.g. "08:00"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_POLL_SECONDS = 30  # min resolution is minutes; 30s keeps drift small


def valid_time(time_str: str) -> bool:
    """True if time_str is a well-formed daily HH:MM."""
    return bool(_TIME_RE.match(time_str))


@dataclass
class ScheduleTask:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    content: str = ""
    time: str = ""           # "HH:MM", fires every day
    last_fired: str = ""     # "YYYY-MM-DD" of the last trigger ("" = never)


def _now() -> datetime:
    """Current local time. Overridable in tests via monkeypatch."""
    return datetime.now()


def load_tasks() -> list[ScheduleTask]:
    """Read tasks from disk. Corrupt or missing file -> empty list."""
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return [ScheduleTask(**t) for t in data]
    except (json.JSONDecodeError, TypeError, KeyError, OSError):
        return []


def save_tasks(tasks: list[ScheduleTask]) -> None:
    """Persist tasks to ~/.corecoder/tasks.json."""
    _TASKS_DIR.mkdir(parents=True, exist_ok=True)
    data = [t.__dict__ for t in tasks]
    TASKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class CronScheduler:
    """Holds tasks, watches the clock on a daemon thread, enqueues due tasks.

    The scheduler itself never calls the LLM.  It only fills ``self.queue``;
    whoever owns the main event loop drains it and runs the agent.
    """

    def __init__(self, poll_seconds: int = _POLL_SECONDS):
        self.tasks: list[ScheduleTask] = load_tasks()
        self.queue: queue.Queue[ScheduleTask] = queue.Queue()
        self._lock = threading.Lock()      # guards tasks
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._poll_seconds = poll_seconds

    # --- public task API (called from the main thread, e.g. crontab tools) ---

    def register_task(self, content: str, time: str) -> str:
        """Add a daily task, persist it, return its task_id."""
        if not valid_time(time):
            raise ValueError(f"invalid time '{time}', expected HH:MM")
        task = ScheduleTask(content=content, time=time)
        with self._lock:
            self.tasks.append(task)
            save_tasks(self.tasks)
        return task.task_id

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by id. Returns True if one was removed."""
        with self._lock:
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t.task_id != task_id]
            if len(self.tasks) != before:
                save_tasks(self.tasks)
        return len(self.tasks) != before

    def list_tasks(self) -> list[ScheduleTask]:
        with self._lock:
            return list(self.tasks)

    # --- trigger side (daemon thread) ---

    def get_due_tasks(self) -> list[ScheduleTask]:
        """Check the clock and fire any task whose time has arrived.

        A task fires when: current HH:MM == task.time AND it has not fired
        today.  Firing marks last_fired=today (persisted) and enqueues it.
        """
        now = _now()
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        due: list[ScheduleTask] = []
        with self._lock:
            for t in self.tasks:
                if t.time == hhmm and t.last_fired != today:
                    t.last_fired = today
                    due.append(t)
            if due:
                save_tasks(self.tasks)
                for t in due:
                    self.queue.put(t)
        return due

    def start(self) -> None:
        """Start the background trigger thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="corecoder-cron", daemon=True)
        self._thread.start()

    def stop(self, join: bool = True) -> None:
        """Stop the background thread."""
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.get_due_tasks()
            except Exception:
                pass  # a bad file or clock issue must never kill the thread
            self._stop.wait(self._poll_seconds)


# lazy module-level singleton, shared across Agent instances
_singleton: CronScheduler | None = None
_singleton_lock = threading.Lock()


def get_scheduler() -> CronScheduler:
    """Return the process-wide scheduler, creating it on first use."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CronScheduler()
        return _singleton
