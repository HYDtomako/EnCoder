"""Tests for the trace view (design_trace_v1.md).

The write side is tested through a real CheckpointManager (the fields have to
land in the file, not in memory), and the read side through plain event lists --
``build_traces`` / ``render_trace`` are pure, so almost nothing here needs an
agent or an LLM. The exception is the teammate attribution (v1 §3.4): that
mapping is the Agent's, so it is exercised through one.
"""

import json
import threading

import pytest
from rich.console import Console

from encoder.agent import Agent
from encoder.checkpoint import (
    CheckpointManager,
    Event,
    Stats,
    should_checkpoint,
    take_changes,
    trigger_for,
)
from encoder.llm import LLM
from encoder.tools.write import WriteFileTool
from encoder.trace import (
    build_traces,
    read_checkpoints,
    read_events,
    render_index,
    render_trace,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _render(trace, full: bool = False) -> str:
    """Render to plain text -- the assertions are about what a reader sees."""
    import io
    console = Console(file=io.StringIO(), width=120, no_color=True)
    console.print(render_trace(trace, full=full))
    return console.file.getvalue()


@pytest.fixture(autouse=True)
def _empty_change_registry():
    """改动的登记表是「每次工具调用取一次」的，而这里的测试会直接调工具
    （不经过 agent loop 的那次取），所以前后各清一次——否则别的测试文件留下的
    一条会被算进来。
    """
    take_changes()
    yield
    take_changes()


def ev(seq: int, type_: str, **fields) -> Event:
    """A synthetic log line. ``ts`` rises with seq so ordering is visible."""
    return Event(seq=seq, ts=f"2026-09-14 10:31:{seq:02d}", type=type_, **fields)


def log(tmp_path, events: list[Event], name: str = "events.jsonl"):
    """Write events to disk exactly as the writer would."""
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e.to_dict(), ensure_ascii=False)
                              for e in events) + "\n", encoding="utf-8")
    return path


def _conversation() -> list[Event]:
    """两轮对话：第一轮「读文件」，第二轮「跑测试失败」。"""
    return [
        ev(1, "session_start", actor="system", data={"session": "s"}),
        ev(2, "user_message", actor="user", input="改一下 edit",
           data={"raw": "改一下 edit", "prelude": [{"kind": "task_reminder", "head": "有 1 个任务"}]}),
        ev(3, "llm_call", actor="lead", status="ok", output="先读一下",
           duration_ms=3120, data={"model": "m", "prompt_tokens": 18422,
                                   "completion_tokens": 210,
                                   "calls": [{"id": "call_ab12", "name": "read_file"}]}),
        ev(4, "tool_done", actor="lead", tool="read_file",
           output="ok", status="ok", call_id="call_ab12",
           data={"args": {"file_path": "encoder/tools/edit.py"}}),
        ev(5, "user_message", actor="user", input="再跑测试", data={"raw": "再跑测试"}),
        ev(6, "tool_done", actor="lead", tool="bash", output="boom",
           status="error", data={"args": {"command": "pytest"}}),
    ]


# --------------------------------------------------------------------------- #
# 读侧:切 turn
# --------------------------------------------------------------------------- #

def test_turns_split_on_user_messages(tmp_path):
    traces = build_traces(_conversation())

    assert [len(t.steps) for t in traces] == [3, 1]      # request 本身是容器头
    assert traces[0].request == "改一下 edit"
    assert traces[1].request == "再跑测试"
    # 第一个 turn 是 [session_start, llm, tool]，第二个只吃下自己那条 tool_done
    assert [s.event.seq for s in traces[0].steps] == [1, 3, 4]
    assert [s.event.seq for s in traces[1].steps] == [6]


def test_events_before_the_first_request_stay_in_the_first_turn(tmp_path):
    """会话开始不该自成一个 turn：那时还没有请求可分。"""
    traces = build_traces(_conversation())

    assert len(traces) == 2
    assert traces[0].steps[0].event.type == "session_start"
    assert traces[0].started_at == "2026-09-14 10:31:02"      # 表头用的是请求时刻


def test_a_turn_ended_by_an_interrupt_does_not_swallow_the_next_one():
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "interrupt", actor="user", tool="bash", data={"pending": ["call_x"]}),
        ev(3, "user_message", actor="user", input="B", data={"raw": "B"}),
    ]
    traces = build_traces(events)

    assert [t.request for t in traces] == ["A", "B"]
    assert [s.event.type for s in traces[0].steps] == ["interrupt"]


# --------------------------------------------------------------------------- #
# 读侧:配对、原因、时长、断点锚点
# --------------------------------------------------------------------------- #

def test_call_id_pairs_a_decision_with_its_execution():
    traces = build_traces(_conversation())
    out = _render(traces[0])

    assert "→ read_file" in out            # 模型决定了要调它
    assert "← call_ab12" in out            # 执行的那条指回同一个 id
    assert "3.1s" in out and "in 18.4k" in out


def test_an_old_log_without_llm_calls_still_renders():
    """旧日志（写侧还没有 llm_call）降级成「未归属的执行」，不报错。"""
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "tool_done", actor="lead", tool="bash", output="ok",
           status="ok", data={"args": {"command": "ls"}}),
    ]
    out = _render_traces(events)

    assert "bash" in out and "←" not in out


def _render_traces(events, full: bool = False) -> str:
    import io
    console = Console(file=io.StringIO(), width=120, no_color=True)
    for t in build_traces(events):
        console.print(render_trace(t, full=full))
    return console.file.getvalue()


def test_reason_is_derived_read_side():
    """原因不落盘：同一句 "no such file or directory" 靠工具名消歧。"""
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "tool_done", actor="lead", tool="edit_file", status="error",
           output="Error: old_string not found in a.py", data={"args": {"file_path": "a.py"}}),
        ev(3, "tool_done", actor="lead", tool="bash", status="error",
           output="bash: no such file or directory",
           data={"args": {"command": "nosuchcmd"}}),
        ev(4, "tool_done", actor="lead", tool="read_file", status="error",
           output="no such file or directory", data={"args": {"file_path": "gone.py"}}),
    ]
    traces = build_traces(events)
    kinds = [s.error["kind"] if s.error else "" for s in traces[0].steps]

    assert kinds == ["stale_view", "env", "not_found"]      # 请求本身不是一步
    out = _render(traces[0])
    assert "[stale_view]" in out and "[env]" in out


def test_repeat_counts_the_same_failure():
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "tool_done", actor="lead", tool="edit_file", status="error",
           output="old_string not found", data={"args": {"file_path": "a.py"}}),
        ev(3, "tool_done", actor="lead", tool="edit_file", status="error",
           output="old_string not found", data={"args": {"file_path": "a.py"}}),
    ]
    steps = build_traces(events)[0].steps

    assert [s.repeat for s in steps] == [1, 2]
    assert "第 2 次" in _render(build_traces(events)[0])


def test_duration_is_shown_in_the_unit_that_reads_best():
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "tool_done", actor="lead", tool="bash", status="ok",
           output="done", duration_ms=120, data={"args": {"command": "ls"}}),
        ev(3, "tool_done", actor="lead", tool="bash", status="ok",
           output="done", duration_ms=4200, data={"args": {"command": "ls"}}),
    ]
    out = _render_traces(events)

    assert "120ms" in out and "4.2s" in out


def test_checkpoints_are_anchored_where_they_were_taken():
    events = _conversation()
    cps = [{"id": "cp_0007", "seq": 4, "trigger": "error", "label": "编辑失败后",
            "parent_id": "cp_0006"}]
    traces = build_traces(events, cps)

    kinds = [s.kind for s in traces[0].steps]
    assert kinds == ["session_start", "llm", "tool", "checkpoint"]
    out = _render(traces[0])
    assert "cp_0007" in out and "trigger=error" in out


def test_workspace_and_focus_are_inherited_forward():
    events = [
        ev(1, "user_message", actor="user", input="A", data={"raw": "A"}),
        ev(2, "tool_done", actor="lead", tool="bash", status="ok",
           output="ok", workspace={"kind": "repo", "root": "/r", "branch": "main"},
           focus={"task_id": "t3", "todo_id": ""}, data={"args": {"command": "ls"}}),
        ev(3, "tool_done", actor="lead", tool="bash", status="ok",
           output="ok", data={"args": {"command": "ls"}}),
    ]
    steps = build_traces(events)[0].steps

    assert steps[1].workspace["branch"] == "main"      # 没盖章的那条也答得出来
    assert steps[1].focus == {"task_id": "t3"}
    assert build_traces(events)[0].task_id == "t3"


# --------------------------------------------------------------------------- #
# 读侧:容错
# --------------------------------------------------------------------------- #

def test_read_events_merges_archive_and_skips_corrupt_lines(tmp_path):
    session = tmp_path / "s"
    (session / "archive").mkdir(parents=True)
    archive = session / "archive" / "events-20260914-000000.jsonl"
    archive.write_text("\n".join(json.dumps(e.to_dict()) for e in
                                 [ev(1, "turn_end"), ev(2, "turn_end")]) + "\n",
                       encoding="utf-8")
    tail = session / "events.jsonl"
    tail.write_text(
        json.dumps(ev(3, "turn_end").to_dict()) + "\n"
        + "{ this line was cut by a kill\n"
        + "\n"
        + json.dumps(ev(4, "turn_end").to_dict()) + "\n",
        encoding="utf-8")

    events = read_events(session)

    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert read_events(tmp_path / "nope") == []      # 没有日志不是错误


def test_an_old_log_still_reads_after_name_and_error_are_gone(tmp_path):
    """v1 删了万能字段 ``name`` / 只读不写的 ``error``，但旧日志得照读（§3.4）。"""
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in [
        {"seq": 1, "ts": "t", "type": "session_start", "actor": "system",
         "name": "sess_9"},
        {"seq": 2, "ts": "t", "type": "todo_changed", "actor": "lead", "name": "t2",
         "status": "in_progress", "data": {"action": "update"}},
        {"seq": 3, "ts": "t", "type": "tool_done", "actor": "lead", "name": "bash",
         "tool": "bash", "output": "ok", "status": "error", "error": "boom"},
    ]) + "\n", encoding="utf-8")

    events = read_events(tmp_path)

    assert events[0].data["session"] == "sess_9"      # name -> 这个类型自己的槽位
    assert events[1].data["todo_id"] == "t2"
    # 工具事件不映射：关于什么早就在 tool 里了；而 error 谁都没读过它写的那份
    step = next(s for s in build_traces(events)[0].steps if s.kind == "tool")
    assert step.event.tool == "bash"
    assert step.error and step.error["kind"]


def test_read_checkpoints_survives_a_broken_index(tmp_path):
    (tmp_path / "index.jsonl").write_text(
        json.dumps({"id": "cp_0001", "seq": 3}) + "\n{bad\n", encoding="utf-8")

    assert [m["id"] for m in read_checkpoints(tmp_path)] == ["cp_0001"]


def test_index_lists_every_turn():
    text = render_index(build_traces(_conversation())).plain

    assert "2 个 turn" in text and "改一下 edit" in text


# --------------------------------------------------------------------------- #
# 写侧:字段真的落到文件里了
# --------------------------------------------------------------------------- #

def test_llm_call_can_never_trigger_a_snapshot():
    """每轮都发的事件要是进了硬触发，就成了「无脑保存」。"""
    assert trigger_for(ev(1, "llm_call")) is None
    assert should_checkpoint(ev(1, "llm_call"), Stats(events_since=99)).should is False


def test_workspace_is_stamped_only_when_it_changes(tmp_path):
    mgr = CheckpointManager(agent=None, base_dir=tmp_path, session_id="s", enabled=True)
    mgr.record("llm_call", actor="lead", data={"calls": []})
    mgr.record("llm_call", actor="lead", data={"calls": []})

    lines = [json.loads(l) for l in
             (tmp_path / "s" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "workspace" in lines[0]
    assert "workspace" not in lines[1]        # 没变就不写


def test_focus_is_stamped_on_change_and_cleared_explicitly(tmp_path):
    mgr = CheckpointManager(agent=None, base_dir=tmp_path, session_id="s", enabled=True)
    mgr.record("todo_changed", actor="lead", status="in_progress",
               data={"action": "update", "todo_id": "t2"})
    mgr.record("llm_call", actor="lead", data={"calls": []})          # 盖章点在这里
    mgr.record("todo_changed", actor="lead", status="completed",
               data={"action": "update", "todo_id": "t2"})
    mgr.record("llm_call", actor="lead", data={"calls": []})

    lines = [json.loads(l) for l in
             (tmp_path / "s" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0].get("focus") is None            # 变的那条本身带的还是旧值
    assert lines[1]["focus"] == {"task_id": "", "todo_id": "t2"}
    # 「清空了」要显式写成空串：空 dict 会被 to_dict 丢掉，读侧就看不见这件事
    assert lines[3]["focus"] == {"task_id": "", "todo_id": ""}


def test_content_arguments_are_clipped_not_stored(tmp_path):
    """write_file 的正文曾经整段抄进日志 —— 现在只留长度和开头。"""
    body = "x = 1\n" * 800
    target = tmp_path / "a.py"
    result = WriteFileTool().execute(file_path=str(target), content=body)
    changes = take_changes()

    mgr = CheckpointManager(agent=None, base_dir=tmp_path / "cp", session_id="s",
                            enabled=True)
    mgr.record("tool_done", actor="lead", tool="write_file",
               output=result, status="ok", call_id="c1", change=changes,
               data={"args": {"file_path": str(target), "content": body}})
    line = json.loads((tmp_path / "cp" / "s" / "events.jsonl")
                      .read_text(encoding="utf-8").splitlines()[0])

    args = line["data"]["args"]
    assert args["content"].startswith("<4800 chars>")
    assert len(json.dumps(line, ensure_ascii=False)) < len(body)
    # 改了什么以差分的形状留下了 —— 体量跟改动走，不跟文件大小走
    assert line["change"][0]["kind"] == "write"
    assert line["change"][0]["added"] == 800
    assert line["change"][0]["patch"].startswith("--- ")


def test_the_change_registry_never_mixes_two_threads():
    """并行工具调用各在自己的线程里记，互不串。"""
    seen = {}

    def work(n):
        from encoder.checkpoint import record_change
        record_change(f"f{n}.py", "edit", patch="@@ -1 +1 @@\n-a\n+b")
        seen[n] = take_changes()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen[0][0]["path"].endswith("f0.py")
    assert seen[1][0]["path"].endswith("f1.py")
    assert take_changes() == []            # 取过就空了


def test_a_teammate_event_is_attributed_to_the_teammate(tmp_path):
    """v1 §3.4 的「修 1」：队友的事件曾经全写成 ``lead``，读侧就分不清谁干的。"""
    agent = Agent(llm=LLM.__new__(LLM), tools=[], memory_enabled=False,
                  checkpoint_enabled=True, checkpoint_dir=str(tmp_path))

    agent._on_team_event("status", name="agent_2", status="work",
                         previous="idle", to="work", task_id="task_9")
    agent._on_team_event("integrate", status="merged")        # 没有 name：Lead 自己干的

    lines = [json.loads(line) for line in
             (tmp_path / agent.checkpoints.session_id / "events.jsonl")
             .read_text(encoding="utf-8").splitlines()]
    work, merged = lines[-2], lines[-1]

    assert work["actor"] == "agent_2"            # 「谁」在 actor 里，不在别处
    assert work["data"]["task_id"] == "task_9"   # 「关于什么」有自己的槽位
    assert work["data"]["from"] == "idle"        # previous -> from（from 是关键字）
    assert merged["actor"] == "lead"
