"""Tests for the Todo/Task system (DESIGN_todo_task_v1.md)."""

import json
import threading

import pytest

from corecoder.task import MAX_ATTEMPTS, PRIORITIES, STATES, TaskManager, TodoList
from corecoder.tools.task import (
    ArchiveTasksTool, CreateTaskTool, DispatchTaskTool, ListTasksTool, UpdateTaskTool,
)
from corecoder.tools.todo import CreateTodoTool, UpdateTodoTool


class _FakeParent:
    """Stand-in for Agent: exposes todos + tasks so tools can be driven standalone."""

    def __init__(self, base_dir):
        self.todos = TodoList()
        self.tasks = TaskManager(base_dir=base_dir)


def _tool(cls, parent):
    tool = cls()
    tool._parent_agent = parent
    return tool


# --------------------------------------------------------------------------- #
# Todo - session-scoped checklist
# --------------------------------------------------------------------------- #

def test_todo_create_batch_pending():
    todos = TodoList()
    created = todos.create(["调研结构", "设计方案"], task_id="abc")
    assert [t.title for t in created] == ["调研结构", "设计方案"]
    assert all(t.status == "pending" for t in created)
    assert [t.todo_id for t in created] == ["t1", "t2"]
    assert todos.list()[0].task_id == "abc"


def test_todo_update_and_invalid_status():
    todos = TodoList()
    todos.create(["a", "b"])
    updated = todos.update("t1", status="in_progress", note="doing")
    assert updated.status == "in_progress"
    assert updated.note == "doing"
    assert todos.update("nope", status="completed") is None
    with pytest.raises(ValueError):
        todos.update("t1", status="bogus")


def test_todo_clear():
    todos = TodoList()
    todos.create(["a", "b", "c"])
    todos.clear()
    assert todos.list() == []


# --------------------------------------------------------------------------- #
# Task create / slug files
# --------------------------------------------------------------------------- #

def test_create_persists_slug_file(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    task = tm.create("实现 dispatch_task 工具")
    # readable slug, not the opaque task_id
    expected = tmp_path / "实现-dispatch-task-工具.json"
    assert expected.exists()
    data = json.loads(expected.read_text(encoding="utf-8"))
    assert data["task_id"] == task.task_id
    assert data["state"] == "pending"
    assert data["priority"] == "normal"
    assert task.task_id  # non-empty
    assert tm.get(task.task_id).task_id == task.task_id


def test_create_slug_collision_gets_suffix(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    a = tm.create("相同的任务描述")
    b = tm.create("相同的任务描述")
    assert a.task_id != b.task_id
    # second file is disambiguated; both readable
    paths = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(paths) == 2
    assert any(name == f"相同的任务描述-{b.task_id[:4]}.json" for name in paths)


def test_create_rejects_blank_description(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    with pytest.raises(ValueError):
        tm.create("   ")


def test_file_slug_falls_back_to_task_id_on_blank():
    from corecoder.task import _file_slug
    assert _file_slug("   ", "abc123") == "abc123"


def test_create_rejects_unknown_dependency(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    with pytest.raises(ValueError):
        tm.create("dep on ghost", blocked_by=["does-not-exist"])


def test_create_rejects_bad_priority(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    with pytest.raises(ValueError):
        tm.create("bad priority", priority="urgent")


# --------------------------------------------------------------------------- #
# blockedBy state machine (add2.0 section 3/4)
# --------------------------------------------------------------------------- #

def test_task_without_deps_can_start(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("standalone")
    ok, blocked = tm.can_start(t.task_id)
    assert ok is True and blocked == []
    tm.mark_in_progress(t.task_id)
    assert tm.get(t.task_id).state == "in_progress"


def test_unfinished_dependency_blocks_start(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    dep = tm.create("依赖任务")
    task = tm.create("主任务", blocked_by=[dep.task_id])
    ok, blocked = tm.can_start(task.task_id)
    assert ok is False
    assert blocked == [dep.task_id]
    with pytest.raises(RuntimeError) as e:
        tm.mark_in_progress(task.task_id)
    assert "dependencies not completed" in str(e.value)
    assert tm.get(task.task_id).state == "pending"


def test_all_dependencies_completed_allows_start(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    dep = tm.create("依赖任务")
    task = tm.create("主任务", blocked_by=[dep.task_id])
    tm.mark_in_progress(dep.task_id)
    tm.mark_completed(dep.task_id)
    ok, blocked = tm.can_start(task.task_id)
    assert ok is True and blocked == []
    tm.mark_in_progress(task.task_id)
    assert tm.get(task.task_id).state == "in_progress"


def test_mark_completed_only_from_in_progress(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("never started")
    with pytest.raises(RuntimeError):
        tm.mark_completed(t.task_id)
    tm.mark_in_progress(t.task_id)
    tm.mark_completed(t.task_id, result="done!")
    task = tm.get(t.task_id)
    assert task.state == "completed"
    assert task.result == "done!"


def test_ready_tasks_sorted_by_priority(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    low = tm.create("low one", priority="low")
    high = tm.create("high one", priority="high")
    blocked = tm.create("blocked one", blocked_by=[low.task_id])
    tm.mark_in_progress(low.task_id)
    tm.mark_completed(low.task_id)   # now 'blocked' has all deps done -> ready
    ready = tm.ready_tasks()
    ids = [t.task_id for t in ready]
    assert low.task_id not in ids        # completed tasks are excluded
    assert high.task_id in ids           # pending, no deps -> ready
    assert blocked.task_id in ids        # pending, deps now completed -> ready
    assert ids[0] == high.task_id        # high priority sorts first
    assert ids[1] == blocked.task_id


def test_priority_update_validation(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("task")
    tm.update(t.task_id, priority="high")
    assert tm.get(t.task_id).priority == "high"
    with pytest.raises(ValueError):
        tm.update(t.task_id, priority="super")


def test_state_sync_is_immediate(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("sync check")
    tm.mark_in_progress(t.task_id)
    data = json.loads((tmp_path / "sync-check.json").read_text(encoding="utf-8"))
    assert data["state"] == "in_progress"
    tm.mark_completed(t.task_id, result="r")
    data = json.loads((tmp_path / "sync-check.json").read_text(encoding="utf-8"))
    assert data["state"] == "completed"
    assert data["result"] == "r"


# --------------------------------------------------------------------------- #
# retry limit + re-entry (MAX_ATTEMPTS)
# --------------------------------------------------------------------------- #

def _fail(task, tm):
    task.last_error = "boom"
    tm.save(task)


def test_retry_allowed_until_max(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("retry me")
    tm.mark_in_progress(t.task_id)          # attempt 1
    _fail(tm.get(t.task_id), tm)
    assert tm.can_retry(t.task_id) is True
    tm.mark_in_progress(t.task_id)          # attempt 2 (retry path)
    assert tm.get(t.task_id).attempts == 2
    _fail(tm.get(t.task_id), tm)
    assert tm.can_retry(t.task_id) is True  # 2 < 3
    tm.mark_in_progress(t.task_id)          # attempt 3
    _fail(tm.get(t.task_id), tm)
    assert tm.can_retry(t.task_id) is False  # 3 >= 3


def test_mark_in_progress_refuses_double_running(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    t = tm.create("running")
    tm.mark_in_progress(t.task_id)
    with pytest.raises(RuntimeError) as e:
        tm.mark_in_progress(t.task_id)
    assert "already in progress" in str(e.value)


# --------------------------------------------------------------------------- #
# dispatch tool
# --------------------------------------------------------------------------- #

def test_dispatch_blocked_task(tmp_path):
    parent = _FakeParent(tmp_path)
    dep = parent.tasks.create("dep")
    task = parent.tasks.create("depends", blocked_by=[dep.task_id])
    tool = _tool(DispatchTaskTool, parent)
    r = tool.execute(task_id=task.task_id)
    assert "dependencies not completed" in r
    assert parent.tasks.get(task.task_id).state == "pending"


def test_dispatch_success_writes_result(monkeypatch, tmp_path):
    parent = _FakeParent(tmp_path)
    task = parent.tasks.create("写测试")
    monkeypatch.setattr(
        "corecoder.tools.task.spawn_subagent", lambda parent_, desc: "fake output")
    tool = _tool(DispatchTaskTool, parent)
    r = tool.execute(task_id=task.task_id)
    assert "completed" in r
    assert "fake output" in r
    t = parent.tasks.get(task.task_id)
    assert t.state == "completed"
    assert t.result == "fake output"


def test_dispatch_failure_marks_last_error_and_stays_in_progress(monkeypatch, tmp_path):
    parent = _FakeParent(tmp_path)
    task = parent.tasks.create("flaky")
    def boom(parent_, desc):
        raise RuntimeError("agent died")
    monkeypatch.setattr("corecoder.tools.task.spawn_subagent", boom)
    tool = _tool(DispatchTaskTool, parent)
    r = tool.execute(task_id=task.task_id)
    assert "Error dispatching" in r
    t = parent.tasks.get(task.task_id)
    assert t.state == "in_progress"
    assert t.last_error == "agent died"
    assert t.attempts == 1


def test_dispatch_hits_max_attempts(monkeypatch, tmp_path):
    parent = _FakeParent(tmp_path)
    task = parent.tasks.create("doomed")
    def boom(parent_, desc):
        raise RuntimeError("agent died")
    monkeypatch.setattr("corecoder.tools.task.spawn_subagent", boom)
    tool = _tool(DispatchTaskTool, parent)
    for _ in range(MAX_ATTEMPTS):
        tool.execute(task_id=task.task_id)
    r = tool.execute(task_id=task.task_id)
    assert "max dispatch attempts" in r
    assert parent.tasks.get(task.task_id).attempts == MAX_ATTEMPTS


def test_dispatch_rerunning_in_progress_refused(tmp_path):
    parent = _FakeParent(tmp_path)
    task = parent.tasks.create("running")
    parent.tasks.mark_in_progress(task.task_id)
    tool = _tool(DispatchTaskTool, parent)
    r = tool.execute(task_id=task.task_id)
    assert "already in progress" in r


# --------------------------------------------------------------------------- #
# archive (root task done -> subgraph archived into .TASK/done/ one file)
# --------------------------------------------------------------------------- #

def test_archive_refuses_unfinished_root(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    root = tm.create("总任务")
    with pytest.raises(RuntimeError) as e:
        tm.archive(root.task_id)
    assert "not completed" in str(e.value)


def test_archive_refuses_unfinished_subgraph(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    sub = tm.create("分支小任务")
    root = tm.create("总任务", blocked_by=[sub.task_id])
    tm.update(root.task_id, state="completed")  # force, leaving sub unfinished
    with pytest.raises(RuntimeError) as e:
        tm.archive(root.task_id)
    assert "subgraph not all completed" in str(e.value)


def test_archive_completed_subgraph_to_single_file(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    b = tm.create("分支B")
    c = tm.create("分支C")
    root = tm.create("总任务", blocked_by=[b.task_id, c.task_id])
    for t in (b, c, root):
        tm.mark_in_progress(t.task_id)
        tm.mark_completed(t.task_id, result="ok")

    archived = tm.archive(root.task_id)
    assert len(archived) == 3
    # active files gone, one aggregate file under done/
    assert len(list(tmp_path.glob("*.json"))) == 0
    done_files = list((tmp_path / "done").glob("*.json"))
    assert len(done_files) == 1
    data = json.loads(done_files[0].read_text(encoding="utf-8"))
    assert len(data) == 3
    assert all(t["state"] == "completed" for t in data)
    assert tm.list() == []


# --------------------------------------------------------------------------- #
# concurrency (prepared for add3.0 parallel agent_team)
# --------------------------------------------------------------------------- #

def test_concurrent_mark_different_tasks(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    tasks = [tm.create(f"并发任务{i}") for i in range(6)]

    def run(t):
        tm.mark_in_progress(t.task_id)
        tm.mark_completed(t.task_id, result="done")

    threads = [threading.Thread(target=run, args=(t,)) for t in tasks]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert all(tm.get(t.task_id).state == "completed" for t in tasks)


# --------------------------------------------------------------------------- #
# end-to-end tools
# --------------------------------------------------------------------------- #

def test_tool_workflow_create_list_dispatch_archive(monkeypatch, tmp_path):
    parent = _FakeParent(tmp_path)
    monkeypatch.setattr(
        "corecoder.tools.task.spawn_subagent", lambda parent_, desc: f"done: {desc}")

    create = _tool(CreateTaskTool, parent)
    sub = create.execute(description="子任务A", priority="high")
    assert "Created task" in sub
    dep_id = parent.tasks.list()[0].task_id

    root = create.execute(description="总任务", blocked_by=[dep_id])
    root_id = parent.tasks.list()[-1].task_id

    lst = _tool(ListTasksTool, parent)
    out = lst.execute()
    assert "子任务A" in out and "总任务" in out and "priority: high" in out

    dispatch = _tool(DispatchTaskTool, parent)
    assert "completed" in dispatch.execute(task_id=dep_id)
    assert "completed" in dispatch.execute(task_id=root_id)

    arch = _tool(ArchiveTasksTool, parent)
    r = arch.execute(root_id=root_id)
    assert "Archived 2 task(s)" in r
    assert parent.tasks.list() == []


def test_update_task_tool(tmp_path):
    parent = _FakeParent(tmp_path)
    task = parent.tasks.create("可调")
    tool = _tool(UpdateTaskTool, parent)
    assert "state=in_progress" in tool.execute(task_id=task.task_id, state="in_progress")
    assert "priority=high" in tool.execute(task_id=task.task_id, priority="high")
    assert "Error" in tool.execute(task_id=task.task_id, state="bogus")


def test_todo_tools(tmp_path):
    parent = _FakeParent(tmp_path)
    create = _tool(CreateTodoTool, parent)
    r = create.execute(titles=["先做A", "再做B"])
    assert "t1" in r and "t2" in r and "pending" in r
    upd = _tool(UpdateTodoTool, parent)
    assert "in_progress" in upd.execute(todo_id="t1", status="in_progress")
    assert "Error" in upd.execute(todo_id="t1", status="bogus")
    assert "Error" in upd.execute(todo_id="t9", status="completed")


# --------------------------------------------------------------------------- #
# proactive work surfacing: unfinished() / pending_reminder() (xuigai2.0)
# --------------------------------------------------------------------------- #

def test_pending_reminder_empty_when_no_tasks(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    assert tm.unfinished() == []
    assert tm.pending_reminder() == ""


def test_pending_reminder_empty_when_all_completed(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    a = tm.create("已完成任务")
    tm.mark_in_progress(a.task_id)
    tm.mark_completed(a.task_id)
    assert tm.unfinished() == []
    assert tm.pending_reminder() == ""


def test_pending_reminder_lists_unfinished(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    done = tm.create("已完成的")
    keep = tm.create("还差一点", priority="high")
    tm.mark_in_progress(done.task_id)
    tm.mark_completed(done.task_id)

    reminder = tm.pending_reminder()
    assert reminder.startswith("[⚠️ 持久化任务提醒]")
    assert keep.task_id in reminder and "还差一点" in reminder
    assert "优先级: high" in reminder
    assert "已完成任务" not in reminder          # completed filtered out
    assert "dispatch_task" in reminder           # hints how to continue

    assert [t.task_id for t in tm.unfinished()] == [keep.task_id]


def test_pending_reminder_caps_at_max_show(tmp_path):
    tm = TaskManager(base_dir=tmp_path)
    for i in range(15):
        tm.create(f"未完成任务{i}")
    reminder = tm.pending_reminder(max_show=8)
    lines = [ln for ln in reminder.splitlines() if ln.strip().startswith("[")]
    assert len(lines) == 8 + 1                   # 8 shown + 1 collapse line
    assert "及另外 7 个未完成任务" in reminder
