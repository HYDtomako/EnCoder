"""Tests for the cron scheduler module and its crontab tools."""

from datetime import datetime

import pytest

import encoder.cron_scheduler as cs
from encoder.cron_scheduler import CronScheduler, valid_time


# --- time validation ---

def test_valid_time_accepts_hhmm():
    assert valid_time("00:00")
    assert valid_time("08:00")
    assert valid_time("23:59")


def test_valid_time_rejects_bad():
    assert not valid_time("25:00")
    assert not valid_time("8:0")
    assert not valid_time("0800")
    assert not valid_time("12:60")
    assert not valid_time("")


# --- register / delete ---

def test_register_task(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler()
    task_id = s.register_task("collect AI news", "08:00")
    assert len(s.list_tasks()) == 1
    assert s.list_tasks()[0].time == "08:00"
    assert s.list_tasks()[0].content == "collect AI news"
    assert task_id == s.list_tasks()[0].task_id


def test_register_task_rejects_bad_time(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler()
    with pytest.raises(ValueError):
        s.register_task("x", "25:00")


def test_delete_task(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler()
    task_id = s.register_task("collect AI news", "08:00")
    assert s.delete_task(task_id) is True
    assert s.delete_task(task_id) is False  # already gone
    assert s.list_tasks() == []


def test_tasks_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler()
    s.register_task("summary", "09:15")

    s2 = CronScheduler()  # fresh instance reads from disk
    assert len(s2.list_tasks()) == 1
    assert s2.list_tasks()[0].content == "summary"


# --- trigger semantics (time injected) ---

def _freeze(monkeypatch, iso: str):
    """Make cron_scheduler._now return a fixed local time."""
    monkeypatch.setattr(cs, "_now", lambda: datetime.fromisoformat(iso))


def test_trigger_fires_and_marks_last_fired(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    _freeze(monkeypatch, "2026-08-21T08:00:00")
    s = CronScheduler()
    s.register_task("daily news", "08:00")

    due = s.get_due_tasks()
    assert len(due) == 1
    assert due[0].content == "daily news"
    # fired -> marked today, so it must not fire again today
    assert s.get_due_tasks() == []
    assert s.list_tasks()[0].last_fired == "2026-08-21"


def test_no_fire_before_scheduled_time(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    _freeze(monkeypatch, "2026-08-21T07:59:00")
    s = CronScheduler()
    s.register_task("daily news", "08:00")
    assert s.get_due_tasks() == []


def test_enqueued_to_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    _freeze(monkeypatch, "2026-08-21T08:00:00")
    s = CronScheduler()
    s.register_task("daily news", "08:00")
    s.get_due_tasks()
    assert not s.queue.empty()
    task = s.queue.get_nowait()
    assert task.content == "daily news"


def test_fires_again_next_day(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler()
    s.register_task("daily news", "08:00")

    _freeze(monkeypatch, "2026-08-21T08:00:00")
    assert len(s.get_due_tasks()) == 1

    _freeze(monkeypatch, "2026-08-22T08:00:00")
    assert len(s.get_due_tasks()) == 1  # a new day -> fires again


def test_start_stop_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    s = CronScheduler(poll_seconds=1)
    s.start()
    assert s._thread is not None and s._thread.is_alive()
    s.stop()
    assert not s._thread.is_alive()


# --- crontab tools ---

def test_crontab_tools_roundtrip(tmp_path, monkeypatch):
    from encoder.tools.crontab import (
        CreateScheduleTool,
        ListSchedulesTool,
        DeleteScheduleTool,
    )

    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    # reset the module-level singleton so tools hit the tmp file
    with cs._singleton_lock:
        cs._singleton = CronScheduler()

    create = CreateScheduleTool()
    out = create.execute(content="collect AI news", time="08:00")
    assert "08:00" in out

    out = ListSchedulesTool().execute()
    assert "collect AI news" in out
    assert "08:00" in out

    task_id = cs.get_scheduler().list_tasks()[0].task_id
    out = DeleteScheduleTool().execute(task_id=task_id)
    assert "Deleted" in out
    assert ListSchedulesTool().execute() == "No scheduled tasks."


def test_crontab_tool_rejects_bad_time(tmp_path, monkeypatch):
    from encoder.tools.crontab import CreateScheduleTool
    monkeypatch.setattr(cs, "TASKS_FILE", tmp_path / "tasks.json")
    with cs._singleton_lock:
        cs._singleton = CronScheduler()
    out = CreateScheduleTool().execute(content="x", time="24:99")
    assert "invalid time" in out


def test_singleton_preserved_across_tools():
    """The crontab tools and Agent must share one scheduler."""
    s1 = cs.get_scheduler()
    s2 = cs.get_scheduler()
    assert s1 is s2