"""Tests for checkpoint / breakpoint recovery (design_ckeckpoint.md).

The policy layer is a pure function, the event log is append-only, and a
snapshot is a materialized view of it -- so almost everything here can be driven
without an LLM or an agent loop.
"""

import copy
import json
import threading
from dataclasses import dataclass, field

import pytest

from encoder.agent import Agent
from encoder.checkpoint import (
    ERROR_EXPIRY_EVENTS,
    HARD_TRIGGERS,
    MILESTONE_TRIGGERS,
    CheckpointManager,
    Event,
    EventLog,
    Stats,
    repair_chain,
    should_checkpoint,
    trigger_for,
)
from encoder.llm import LLMResponse, ScriptedLLM
from encoder.task import TaskManager, TodoList

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

@dataclass
class _Todo:
    """Mirrors encoder.task.Todo: the state collector uses asdict(), so a
    dataclass here is what makes the test exercise the real path."""
    todo_id: str
    title: str
    status: str = "pending"


@dataclass
class _Task:
    task_id: str
    state: str = "in_progress"
    description: str = "demo"
    assignee: str | None = None
    priority: str = "normal"
    blockedBy: list = field(default_factory=list)
    note: str = ""


class _FakeTodos:
    def __init__(self, items=None):
        self._items = items or []

    def list(self):
        return list(self._items)


class _FakeTasks:
    def __init__(self, tasks=None):
        self._tasks = tasks or []

    def list(self):
        return list(self._tasks)


class _FakeAgent:
    def __init__(self, todos=None, tasks=None, team=None):
        self.messages = []
        self.todos = _FakeTodos(todos)
        self.tasks = _FakeTasks(tasks)
        self.team = team
        self.llm = None


@pytest.fixture()
def mgr(tmp_path):
    agent = _FakeAgent(todos=[_Todo("t1", "写测试", "in_progress")],
                       tasks=[_Task("task_1")])
    return CheckpointManager(agent=agent, base_dir=tmp_path, session_id="session_test",
                             enabled=True, keep=3, max_checkpoints=50)


# --------------------------------------------------------------------------- #
# policy - pure function, no I/O
# --------------------------------------------------------------------------- #

def test_read_only_tools_never_trigger():
    for tool in ("read_file", "grep", "glob", "list_tasks"):
        d = should_checkpoint(Event(type="tool_done", tool=tool), Stats())
        assert not d.should, tool
        assert trigger_for(Event(type="tool_done", tool=tool)) is None


def test_irreversible_tools_trigger_but_are_throttled():
    event = Event(type="tool_done", tool="write_file")
    now = 1000.0
    # fresh session (last_at == 0 means "never"): a soft trigger fires
    assert should_checkpoint(event, Stats(events_since=0), now=now).should
    # right after a snapshot, with only a couple of events since: throttled
    just_snapped = Stats(events_since=2, last_at=now)
    assert not should_checkpoint(event, just_snapped, now=now).should
    # ... until enough events pile up
    assert should_checkpoint(event, Stats(events_since=5, last_at=now), now=now).should
    # ... or enough time passes, whichever comes first
    assert should_checkpoint(event, Stats(events_since=1, last_at=now - 61), now=now).should


def test_hard_triggers_bypass_the_throttle():
    for event_type in ("turn_end", "compress", "interrupt", "approval", "rewind", "manual"):
        d = should_checkpoint(Event(type=event_type), Stats(events_since=0, last_at=0.0))
        assert d.should, event_type
        assert d.trigger == event_type


def test_disabled_and_busy_short_circuit():
    event = Event(type="turn_end")
    assert not should_checkpoint(event, Stats(), enabled=False).should
    assert not should_checkpoint(event, Stats(writing=True)).should


def test_unknown_event_type_never_triggers():
    assert not should_checkpoint(Event(type="something_new"), Stats()).should


def test_task_done_needs_a_real_transition():
    done = Event(type="task_changed", data={"from": "in_progress", "to": "completed"})
    again = Event(type="task_changed", data={"from": "completed", "to": "completed"})
    assert trigger_for(done) == "task_done"
    assert trigger_for(again) == "state"


def test_teammate_only_milestones_trigger():
    for action in ("spawn", "release", "integrate"):
        assert trigger_for(Event(type="teammate_changed", data={"action": action})) == "teammate"
    # a teammate moving idle -> work does not snapshot the Lead's state
    assert trigger_for(Event(type="teammate_changed", data={"action": "work"})) is None


def test_milestones_are_a_subset_of_hard_triggers():
    """Compaction protects milestones; a milestone nobody triggers is dead weight."""
    assert MILESTONE_TRIGGERS <= HARD_TRIGGERS


# --------------------------------------------------------------------------- #
# event log
# --------------------------------------------------------------------------- #

def test_append_is_monotonic_and_survives_reload(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(5):
        log.emit("tool_done", tool="read_file", name=str(i))
    assert log.seq == 5

    reloaded = EventLog(tmp_path / "events.jsonl")
    assert reloaded.seq == 5            # seq resumes rather than restarting
    assert [e.seq for e in reloaded.read()] == [1, 2, 3, 4, 5]


def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.emit("turn_end")
    with path.open("a", encoding="utf-8") as f:
        f.write("{ this is not json\n")     # torn write, mid-crash
        f.write("\n")                        # blank line
    log.emit("turn_end")

    events = log.read()
    assert [e.seq for e in events] == [1, 2], "a bad line must not cost the good ones"
    assert EventLog(path).seq == 2, "seq resumes from the last parseable line"


def test_concurrent_appends_never_duplicate_a_seq(tmp_path):
    """Teammates log from their own threads while the Lead logs from the main one."""
    log = EventLog(tmp_path / "events.jsonl")

    def work(n):
        for i in range(25):
            log.emit("tool_done", actor=f"agent_{n}", tool="read_file", name=str(i))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = [e.seq for e in log.read()]
    assert len(seqs) == 100
    assert len(set(seqs)) == 100          # no duplicates


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #

def test_snapshot_writes_three_state_layers(mgr):
    mgr.agent.messages.append({"role": "user", "content": "hi"})
    cp_id = mgr.snapshot(trigger="manual", label="打点")
    assert cp_id == "cp_0001"

    cp = mgr.load(cp_id)
    assert cp["meta"]["trigger"] == "manual"
    assert cp["meta"]["label"] == "打点"
    assert cp["state"]["agent"]["messages"][0]["content"] == "hi"
    assert cp["state"]["agent"]["todos"][0]["todo_id"] == "t1"
    assert cp["state"]["agent"]["tasks"][0]["task_id"] == "task_1"
    assert "env" in cp["state"] and "execution" in cp["state"]
    # files are pointers, never contents
    assert "content" not in cp["state"]["env"]


def test_fingerprint_dedupe_skips_an_unchanged_state(mgr):
    mgr.agent.messages.append({"role": "user", "content": "hi"})
    assert mgr.snapshot(trigger="turn_end") is not None
    assert mgr.snapshot(trigger="turn_end") is None       # nothing changed
    mgr.agent.messages.append({"role": "assistant", "content": "done"})
    assert mgr.snapshot(trigger="turn_end") is not None   # now it did


def test_forced_triggers_ignore_dedupe(mgr):
    """'I was interrupted here' is worth recording even when nothing changed."""
    mgr.agent.messages.append({"role": "user", "content": "hi"})
    assert mgr.snapshot(trigger="turn_end") is not None
    assert mgr.snapshot(trigger="interrupt") is not None


def test_parent_chain_links_each_snapshot_to_the_previous(mgr):
    ids = []
    for i in range(3):
        mgr.agent.messages.append({"role": "assistant", "content": f"step{i}"})
        ids.append(mgr.snapshot(trigger="turn_end", force=True))

    metas = {m["id"]: m for m in mgr.list_checkpoints()}
    assert metas[ids[0]]["parent_id"] == ""
    assert metas[ids[1]]["parent_id"] == ids[0]
    assert metas[ids[2]]["parent_id"] == ids[1]
    assert mgr.head_id() == ids[2]


def test_index_is_rebuildable_and_never_authoritative(mgr):
    mgr.agent.messages.append({"role": "user", "content": "hi"})
    mgr.snapshot(trigger="manual")
    index = mgr.dir / "index.jsonl"
    index.unlink()

    metas = mgr.list_checkpoints()          # rebuilt from cp_*.json
    assert len(metas) == 1
    assert index.exists()
    assert json.loads(index.read_text(encoding="utf-8").strip())["id"] == "cp_0001"


def test_restore_appends_and_never_rewrites_history(mgr):
    """Rewinding is an append, never an edit: old records stay byte-identical."""
    mgr.agent.messages.append({"role": "user", "content": "one"})
    first = mgr.snapshot(trigger="manual", force=True)
    mgr.agent.messages.append({"role": "assistant", "content": "two"})
    second = mgr.snapshot(trigger="manual", force=True)
    before = {cid: mgr.dir.joinpath(f"{cid}.json").read_text(encoding="utf-8")
              for cid in (first, second)}

    def apply(restored):
        mgr.agent.messages[:] = restored.messages

    restored = mgr.restore(first, apply=apply)

    # the state handed back is the checkpoint that was named
    assert restored.messages == [{"role": "user", "content": "one"}]
    for cid, text in before.items():
        assert mgr.dir.joinpath(f"{cid}.json").read_text(encoding="utf-8") == text
    # the rewind is recorded as a new event, and as a new recovery point
    # appended to the chain -- so a restore is itself undoable: the checkpoint
    # the user was on (`second`) is still there and still describes the state
    # they left, even though the head has moved on
    assert any(e.type == "rewind" for e in mgr.log.read())
    assert mgr.head_id() not in (first, second)
    assert mgr.load(mgr.head_id())["meta"]["parent_id"] == second


def test_rewind_snapshot_captures_the_restored_state(mgr):
    """The rewind cp must describe where the agent *is*, not where it was.

    v1 snapshotted before the caller applied the state, so the new head held the
    pre-restore messages -- and restoring that head silently undid the restore
    (v2 §11.4).
    """
    mgr.agent.messages.append({"role": "user", "content": "one"})
    first = mgr.snapshot(trigger="manual", force=True)
    mgr.agent.messages.append({"role": "assistant", "content": "two"})
    mgr.snapshot(trigger="manual", force=True)

    mgr.restore(first, apply=lambda r: mgr.agent.messages.__setitem__(
        slice(None), r.messages))

    head = mgr.load(mgr.head_id())
    assert head["state"]["agent"]["messages"] == [{"role": "user", "content": "one"}]
    # and the head has to agree with the live agent, or /checkpoint list lies
    assert head["state"]["agent"]["messages"] == mgr.agent.messages


def test_restore_without_apply_writes_no_snapshot(mgr):
    """A caller that has not moved the agent gets no recovery point for it."""
    first = mgr.snapshot(trigger="manual", force=True)
    mgr.agent.messages.append({"role": "assistant", "content": "two"})
    second = mgr.snapshot(trigger="manual", force=True)

    mgr.restore(first)

    assert mgr.head_id() == second
    assert any(e.type == "rewind" for e in mgr.log.read())


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #

def test_repair_chain_backfills_unanswered_tool_calls():
    broken = [{"role": "assistant", "content": None,
               "tool_calls": [{"id": "c1", "function": {"name": "bash"}}]}]
    repaired = repair_chain(broken)
    assert repaired[-1] == {"role": "tool", "tool_call_id": "c1",
                            "content": "[interrupted]"}
    # already-answered calls are left alone
    ok = repaired + [{"role": "assistant", "content": "done"}]
    assert repair_chain(ok) == ok


def test_restore_repairs_a_snapshot_taken_mid_tool_execution(mgr):
    """A snapshot can land exactly where a crash did -- the chain must come back valid."""
    mgr.agent.messages.append({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c9", "function": {"name": "bash"}}],
    })
    cp_id = mgr.snapshot(trigger="interrupt", force=True)

    restored = mgr.restore(cp_id)
    assert [m["role"] for m in restored.messages][-2:] == ["assistant", "tool"]
    assert restored.messages[-1]["content"] == "[interrupted]"


def test_handoff_points_at_work_and_refuses_to_guess(mgr):
    mgr.agent.messages.append({"role": "user", "content": "hi"})
    cp_id = mgr.snapshot(trigger="manual", label="改 schema 之前")
    restored = mgr.restore(cp_id)

    handoff = restored.handoff
    assert "断点恢复" in handoff
    assert "改 schema 之前" in handoff
    assert "t1" in handoff and "写测试" in handoff        # todo focus
    assert "task_1" in handoff                            # unfinished task
    # it must tell the agent to re-orient rather than assume it already knows
    assert "请先做" in handoff


def test_restore_of_unknown_id_returns_none(mgr):
    assert mgr.restore("cp_9999") is None


# --------------------------------------------------------------------------- #
# compaction
# --------------------------------------------------------------------------- #

def _fill(mgr, n, trigger="tool"):
    for i in range(n):
        mgr.agent.messages.append({"role": "assistant", "content": f"step{i}"})
        mgr.snapshot(trigger=trigger, force=True)


def test_compact_merges_old_snapshots_and_archives_originals(mgr):
    _fill(mgr, 12)
    assert mgr.count() == 12

    mgr.compact(keep=3)

    metas = mgr.list_checkpoints()
    # baseline + merged + the most recent 3 that compaction always keeps
    assert len(metas) == 5
    assert metas[0]["id"] == "cp_0001"
    merged = [m for m in metas if m.get("replaces")]
    assert len(merged) == 1
    assert len(merged[0]["replaces"]) == 8        # cp_0002 .. cp_0009

    archived = {p.name for p in (mgr.dir / "archive").iterdir()}
    for replaced in merged[0]["replaces"]:
        assert f"{replaced}.json" in archived, "originals are archived, never deleted"


def test_compacted_ids_keep_lexical_order_equal_to_chronological(mgr):
    """A fresh id would sort to the end and drop the merge out of the timeline."""
    _fill(mgr, 12)
    mgr.compact(keep=3)
    ids = [m["id"] for m in mgr.list_checkpoints()]
    assert ids == sorted(ids)


def test_compact_never_merges_milestones(mgr):
    for i in range(8):
        mgr.agent.messages.append({"role": "assistant", "content": f"step{i}"})
        mgr.snapshot(trigger="turn_end", force=True)     # every one is a milestone
    mgr.compact(keep=3)

    triggers = [m["trigger"] for m in mgr.list_checkpoints()]
    assert set(triggers) == {"turn_end"}, "milestones must survive compaction"
    assert mgr.count() == 8, "a run of one is just a rename -- nothing to do"


def test_compacted_snapshot_still_restores(mgr):
    _fill(mgr, 12)
    mgr.compact(keep=3)
    merged = next(m for m in mgr.list_checkpoints() if m.get("replaces"))

    restored = mgr.restore(merged["id"])
    assert restored is not None
    assert "Context compressed" in restored.messages[0]["content"]


def test_compaction_trims_the_event_log_without_losing_it(mgr):
    for i in range(20):
        mgr.record("tool_done", tool="write_file", name=f"f{i}.py")
        mgr.agent.messages.append({"role": "assistant", "content": f"step{i}"})
        mgr.snapshot(trigger="tool", force=True)

    assert len(mgr.log.read()) == 20
    mgr.compact(keep=3)

    live = mgr.log.read()
    assert len(live) < 20, "the live log must stay bounded"
    archived = [p for p in (mgr.dir / "archive").iterdir() if p.name.startswith("events-")]
    assert len(archived) == 1
    assert mgr.log.seq == 20, "seq continuity survives the cut"

    mgr.record("turn_end")                  # and the log is still writable
    assert mgr.log.seq == 21


def test_auto_compaction_triggers_above_the_max(mgr):
    mgr.max_checkpoints = 5
    mgr.keep = 2
    _fill(mgr, 8)
    assert mgr.count() <= 5, "crossing the cap compacts automatically"


# --------------------------------------------------------------------------- #
# disabled
# --------------------------------------------------------------------------- #

def test_disabled_manager_has_zero_side_effects(tmp_path):
    agent = _FakeAgent()
    mgr = CheckpointManager(agent=agent, base_dir=tmp_path, enabled=False)

    assert mgr.record("turn_end") is None
    assert mgr.snapshot(trigger="manual") is None
    assert list(tmp_path.iterdir()) == [], "disabled must not even create a dir"


# --------------------------------------------------------------------------- #
# hooks into the real Todo/Task managers
# --------------------------------------------------------------------------- #

def test_todo_on_change_reports_creates_and_updates(tmp_path):
    seen = []
    todos = TodoList()
    todos.on_change = lambda action, todo: seen.append((action, todo.todo_id))
    todos.create(["a", "b"])
    todos.update("t1", status="completed")

    assert seen == [("create", "t2"), ("update", "t1")]


def test_task_on_change_reports_the_transition(tmp_path):
    seen = []
    tasks = TaskManager(base_dir=tmp_path / ".TASK")
    tasks.on_change = lambda task, previous: seen.append((previous, task.state))

    t = tasks.create("做点事")
    tasks.update(t.task_id, state="in_progress")

    assert seen == [(None, "pending"), ("pending", "in_progress")]


def test_todo_list_restore_keeps_ids_unique(tmp_path):
    todos = TodoList()
    todos.create(["a", "b", "c"])
    snapshot = [{"todo_id": t.todo_id, "title": t.title, "status": t.status}
                for t in todos.list()]

    fresh = TodoList()
    assert fresh.restore(snapshot) == 3
    assert [t.todo_id for t in fresh.list()] == ["t1", "t2", "t3"]
    # a new todo must not collide with a restored one
    assert fresh.create(["d"])[0].todo_id == "t4"


def test_a_watching_task_manager_ignores_corrupt_files(tmp_path):
    """Observation hooks must never break the mutation they observe."""
    base = tmp_path / ".TASK"
    tasks = TaskManager(base_dir=base)
    tasks.on_change = lambda task, previous: None
    t = tasks.create("x")
    (base / "broken.json").write_text("{not json", encoding="utf-8")

    tasks.update(t.task_id, state="in_progress")     # must not raise
    assert tasks.get(t.task_id).state == "in_progress"


# --------------------------------------------------------------------------- #
# /compact must not bypass the safety net
# --------------------------------------------------------------------------- #

def _agent_that_will_truncate(tmp_path):
    """A real Agent with a context small enough that compression really fires."""
    agent = Agent(llm=ScriptedLLM([]), tools=[], max_context_tokens=200,
                  memory_enabled=False, checkpoint_enabled=True,
                  checkpoint_dir=str(tmp_path))
    # 12 plain turns of ~55 tokens each: past the summarize threshold (140) and
    # past keep_recent (8), so layer 2 -- the irreversible one -- is reached.
    for i in range(12):
        agent.messages.append({"role": "user", "content": f"第{i}轮 " + "细节" * 80})
    return agent


def test_compact_snapshots_the_work_state_before_truncating(tmp_path):
    """The design's headline requirement: the pre-truncation work_state survives.

    ``/compact`` used to call ``context.maybe_compress`` directly, and the
    context layer has no hooks -- so the snapshot that ``Agent.maybe_compress``
    takes right before layers 2/3 clear ``messages`` was silently skipped, and
    a deliberate truncation was the one truncation you could not recover from.
    """
    agent = _agent_that_will_truncate(tmp_path)
    before = copy.deepcopy(agent.messages)

    assert agent.maybe_compress() is True
    assert len(agent.messages) < len(before), "the live context really was truncated"

    cps = [c for c in agent.checkpoints.list_checkpoints()
           if c["trigger"] == "compress"]
    assert cps, "compression must leave a snapshot behind"
    state = agent.checkpoints.load(cps[0]["id"])["state"]
    assert state["agent"]["messages"] == before, (
        "the snapshot must hold the messages as they were *before* the clear()"
    )


def test_the_pre_truncation_snapshot_is_what_a_restore_hands_back(tmp_path):
    """That snapshot is not just recorded -- it is usable. Restoring it puts the
    compressed-away turns back into the live history."""
    agent = _agent_that_will_truncate(tmp_path)
    before = copy.deepcopy(agent.messages)
    agent.maybe_compress()

    cps = [c for c in agent.checkpoints.list_checkpoints()
           if c["trigger"] == "compress"]
    ok, _ = agent.restore_state(cps[0]["id"])
    assert ok
    assert agent.messages == before


# --------------------------------------------------------------------------- #
# v2: continuation prefix (review01 §1)
# --------------------------------------------------------------------------- #

def test_continuation_prefix_is_idempotent():
    """wrap∘wrap == wrap. Restoring five times cannot stack five prefixes."""
    from encoder.checkpoint import is_continue_only, strip_continue, wrap_continue

    wrapped = wrap_continue("修好登录 bug")
    assert wrapped == "继续：修好登录 bug"
    assert wrap_continue(wrapped) == wrapped
    assert wrap_continue(wrap_continue(wrapped)) == wrapped
    # strip is the load-bearing half: it eats *every* marker, not just one
    assert strip_continue("继续：继续吧： 修好登录 bug") == "修好登录 bug"
    assert is_continue_only("继续")


def test_continue_only_needs_no_instruction_of_its_own():
    from encoder.checkpoint import is_continue_only

    for bare in ("继续", "继续吧", "continue", " 接着 ", "继续："):
        assert is_continue_only(bare), bare
    # a real request is not a bare continue -- it must not be rewritten
    for real in ("修改 schema", "继续：改 schema", "别继续了，回滚"):
        assert not is_continue_only(real), real


def test_bare_continue_is_rewritten_to_the_pending_task(mgr):
    mgr.record("user_message", actor="user", input="修好登录 bug")
    cp_id = mgr.snapshot(trigger="manual", force=True)
    mgr.restore(cp_id)

    # a real instruction outranks the pending task and is passed through
    assert mgr.resume_for_input("改 schema") == "改 schema"
    # ...and the bare continue still carries the original task afterwards
    assert mgr.resume_for_input("继续") == "继续：修好登录 bug"
    # consumed: there is nothing left to wrap a second time
    assert mgr.resume_for_input("继续") == "继续"


def test_restoring_twice_cannot_stack_the_prefix(mgr):
    """The failure mode review01 §1 names: restore, continue, restore, continue."""
    mgr.record("user_message", actor="user", input="修好登录 bug")
    cp_id = mgr.snapshot(trigger="manual", force=True)

    mgr.restore(cp_id)
    first = mgr.resume_for_input("继续")
    mgr.restore(cp_id)
    second = mgr.resume_for_input("继续：继续")

    assert first == second == "继续：修好登录 bug"


# --------------------------------------------------------------------------- #
# v2: what failed, and why (review01 §2 / design_process 04 new_review)
# --------------------------------------------------------------------------- #

def _fail(mgr, tool="edit_file", target="encoder/agent.py", output="old_string not found"):
    mgr.record("tool_done", actor="lead", tool=tool, name=tool, status="error",
               output=output, data={"args": {"file_path": target}})


def _head_execution(mgr) -> dict:
    return mgr.load(mgr.head_id())["state"]["execution"]


def test_a_failure_records_what_it_was_doing_and_why_it_stopped(mgr):
    """error_task alone is the v1 bug: it knows what, not why it stopped."""
    _fail(mgr)
    err = _head_execution(mgr)["last_error"]
    assert err["task"]["tool"] == "edit_file"
    assert err["task"]["target"] == "encoder/agent.py"
    assert err["reason"]["kind"] == "stale_view"
    assert err["reason"]["text"] and "old_string" not in err["reason"]["text"]
    assert err["resolved"] is False
    assert err["seq"] and err["raw"]


def test_the_reason_is_a_closed_set_of_kinds():
    from encoder.checkpoint import REASON_KINDS, classify_error

    cases = {
        "old_string not found in file": "stale_view",
        "merge conflict in encoder/agent.py": "conflict",
        "Permission denied": "permission",
        "command not found: ruff": "env",
        "Command timed out after 120s": "timeout",
        "no such file or directory": "not_found",
    }
    for output, kind in cases.items():
        got = classify_error("edit_file", output)
        assert got["kind"] == kind, (output, got)
        assert got["kind"] in REASON_KINDS
    # the tool disambiguates the one phrase that means two things
    assert classify_error("bash", "no such file or directory")["kind"] == "env"
    # what the rules cannot place stays unknown -- and that is the signal to
    # extend the rule set, not a silent gap
    assert classify_error("edit_file", "something inscrutable")["kind"] == "unknown"


def test_a_later_success_retires_the_failure(mgr):
    _fail(mgr)
    mgr.record("tool_done", actor="lead", tool="edit_file", name="edit_file",
               status="ok", output="ok", data={"args": {"file_path": "encoder/agent.py"}})
    # a snapshot taken *after* the fix is the one that may claim it is fixed
    mgr.snapshot(trigger="turn_end", force=True)

    execu = _head_execution(mgr)
    assert execu["last_error"] is None
    assert execu["errors"] == []
    assert "上次为什么停下" not in mgr.render_handoff(mgr.load(mgr.head_id()))


def test_an_ancient_failure_is_downgraded_to_one_line(mgr):
    """Expiry, not deletion: it survives, it just stops leading the handoff.

    Repeating an ancient failure on every restore would turn "don't fix it
    twice" into "keep being reminded of something nobody cares about".
    """
    _fail(mgr)
    cp_id = mgr.snapshot(trigger="manual", force=True)
    for _ in range(ERROR_EXPIRY_EVENTS + 1):
        mgr.log.emit("tool_done", tool="read_file", status="ok")

    restored = mgr.restore(cp_id)
    assert "历史失败（已过期" in restored.handoff
    assert "上次为什么停下" not in restored.handoff
    assert "不要再原样重跑同一条命令" not in restored.handoff


def test_a_recent_failure_is_never_downgraded(mgr):
    """The other half of the threshold: it must not fire while the failure is
    still the reason the user is restoring at all."""
    _fail(mgr)
    cp_id = mgr.snapshot(trigger="manual", force=True)
    for _ in range(3):
        mgr.log.emit("tool_done", tool="read_file", status="ok")

    restored = mgr.restore(cp_id)
    assert "上次为什么停下" in restored.handoff
    assert "已过期" not in restored.handoff


def test_a_snapshot_taken_before_the_fix_keeps_the_historical_verdict(mgr):
    """The flip side: a snapshot is a materialization of its moment, and the
    file must keep saying what was true then. Undoing that is why the re-check
    at restore time exists instead (v2 §11.2d)."""
    _fail(mgr)
    cp_id = mgr.snapshot(trigger="manual", force=True)
    mgr.record("tool_done", actor="lead", tool="edit_file", name="edit_file",
               status="ok", output="ok", data={"args": {"file_path": "encoder/agent.py"}})

    stamped = mgr.load(cp_id)["state"]["execution"]["last_error"]
    assert stamped and stamped["resolved"] is False


def test_a_success_elsewhere_does_not_retire_it(mgr):
    """Retirement is per tool+target; writing some other file fixes nothing."""
    _fail(mgr)
    mgr.record("tool_done", actor="lead", tool="edit_file", name="edit_file",
               status="ok", output="ok", data={"args": {"file_path": "encoder/task.py"}})
    assert _head_execution(mgr)["last_error"] is not None


def test_restore_recomputes_resolved_against_the_log_tail(mgr):
    """The anti-re-fix rule, and the reason it cannot be a stored boolean.

    The snapshot is a materialization of a moment; the fix may have landed in
    the seconds after it. Recomputing at restore time is the whole point --
    otherwise the recovered agent re-fixes what it already fixed before crashing.
    """
    _fail(mgr)
    cp_id = mgr.snapshot(trigger="manual", force=True)
    assert mgr.load(cp_id)["state"]["execution"]["last_error"], "snapshot says broken"

    # ...and then, before the process died, success
    mgr.record("tool_done", actor="lead", tool="edit_file", name="edit_file",
               status="ok", output="ok", data={"args": {"file_path": "encoder/agent.py"}})

    restored = mgr.restore(cp_id)
    assert restored.last_error is None
    assert "上次为什么停下" not in restored.handoff


def test_repeat_counts_up_and_the_handoff_escalates(mgr):
    for _ in range(3):
        _fail(mgr)
    cp_id = mgr.snapshot(trigger="manual", force=True)
    restored = mgr.restore(cp_id)

    assert restored.last_error["repeat"] == 3
    assert "同一处第 3 次" in restored.handoff
    assert "不要再原样重跑同一条命令" in restored.handoff


def test_only_the_first_failure_is_a_hard_trigger():
    """A loop must not be allowed to write a snapshot per iteration."""
    first = Event(type="tool_done", tool="edit_file", status="error")
    again = Event(type="tool_done", tool="edit_file", status="error", data={"repeat": 1})
    throttled = Stats(events_since=0, last_at=1000.0)
    assert should_checkpoint(first, throttled, now=1000.0).should
    assert not should_checkpoint(again, throttled, now=1000.0).should


def test_only_the_first_failure_is_a_hard_trigger_but_a_read_only_failure_too():
    """The "why am I stuck" snapshot is worth taking even for a read-only tool."""
    read_only_fail = Event(type="tool_done", tool="read_file", status="error")
    assert trigger_for(read_only_fail) == "error"
    # ...while a read-only *success* still never triggers anything
    assert trigger_for(Event(type="tool_done", tool="read_file", status="ok")) is None


def test_a_pending_approval_is_not_a_failure():
    """Waiting for a human is not being stuck; it must not enter last_error."""
    event = Event(type="tool_done", tool="bash", status="pending")
    assert trigger_for(event) == "tool"      # bash, but not via the error branch


# --------------------------------------------------------------------------- #
# v2: what to continue (review01 §2 second half / §11.3)
# --------------------------------------------------------------------------- #

def test_resume_recovers_the_review_command_from_a_pending_call(mgr):
    """The v1 gap: only the tool_call_id was stored, so "continue the review"
    could not say *what* to review or *which* command to run."""
    mgr.agent.messages.append({"role": "user", "content": "让 rev_2 审一下这次改动"})
    mgr.agent.messages.append({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "review_teammate",
                                     "arguments": json.dumps({"name": "rev_2"})}}],
    })
    cp_id = mgr.snapshot(trigger="manual", force=True)
    execu = mgr.load(cp_id)["state"]["execution"]

    assert execu["pending_tool_call"]["tool"] == "review_teammate"
    assert execu["pending_tool_call"]["args"] == {"name": "rev_2"}
    assert execu["resume"]["kind"] == "review"
    assert execu["resume"]["next"]["args"] == {"name": "rev_2"}

    restored = mgr.restore(cp_id)
    assert "review_teammate" in restored.handoff
    assert "不要重新派一个队友" in restored.handoff


def test_an_answered_call_is_not_pending(mgr):
    mgr.agent.messages.append({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
    })
    mgr.agent.messages.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    cp_id = mgr.snapshot(trigger="manual", force=True)
    assert not mgr.load(cp_id)["state"]["execution"].get("pending_tool_call")


def test_the_model_can_name_the_next_step_itself(mgr):
    """`checkpoint(label, next=...)`: the ⑥ boundary only the model can see."""
    from encoder.tools.checkpoint import CheckpointTool

    tool = CheckpointTool()
    tool._parent_agent = mgr.agent
    mgr.agent.checkpoints = mgr
    out = tool.execute("重构完成", next="review_teammate",
                       next_args={"name": "rev_1"}, reason="改完 schema，让队友复查")
    assert "已记录断点" in out

    cp_id = mgr.head_id()
    execu = mgr.load(cp_id)["state"]["execution"]
    assert execu["resume"]["kind"] == "review"
    assert execu["resume"]["next"]["tool"] == "review_teammate"
    assert execu["resume"]["next"]["args"] == {"name": "rev_1"}

    restored = mgr.restore(cp_id)
    assert "改完 schema，让队友复查" in restored.handoff     # intent, model-authored
    assert "review_teammate" in restored.handoff


def test_a_checkpoint_without_next_still_records_the_label(mgr):
    from encoder.tools.checkpoint import CheckpointTool

    tool = CheckpointTool()
    tool._parent_agent = mgr.agent
    mgr.agent.checkpoints = mgr
    assert "已记录断点" in tool.execute("只是打个点")
    assert mgr.load(mgr.head_id())["meta"]["label"] == "只是打个点"
    assert not mgr.load(mgr.head_id())["meta"].get("resume")


def test_resume_descriptors_are_readable(mgr):
    """`/checkpoint list` and `show` render these; they must never explode."""
    from encoder.checkpoint import describe_error, describe_resume

    assert describe_error(None) == ""
    assert describe_resume(None) == ""
    assert describe_error({"task": {}, "reason": {}}).startswith("?")
    _fail(mgr)
    line = describe_error(_head_execution(mgr)["last_error"])
    assert "stale_view" in line and "encoder/agent.py" in line and "seq=" in line
    resume = describe_resume({"kind": "review", "text": "审一下",
                              "next": {"tool": "review_teammate", "args": {"name": "r"}},
                              "generation": 2})
    assert "review" in resume and "已恢复 2 次" in resume


def test_every_resume_kind_is_declared():
    """The handoff branches on this set; an undeclared kind must not leak out."""
    from encoder.checkpoint import RESUME_KINDS

    assert {"task", "review", "tool", "approval", "blocked"} <= RESUME_KINDS


# --------------------------------------------------------------------------- #
# v2: errors survive the key nodes (design_process 04 new_review point 3)
# --------------------------------------------------------------------------- #

def test_compaction_carries_unresolved_errors_forward(mgr):
    """`_merge_run` took only the last member's state, so a failure in an
    earlier segment vanished -- and with it "why am I stuck" (v2 §11.2e)."""
    _fill(mgr, 3, trigger="tool")
    _fail(mgr, output="old_string not found in the file")
    _fill(mgr, 8, trigger="tool")

    mgr.compact(keep=3)
    merged = next(m for m in mgr.list_checkpoints() if m.get("replaces"))
    errs = mgr.load(merged["id"])["state"]["execution"]["errors"]

    assert errs, "the unresolved failure must survive compaction"
    assert errs[0]["reason"]["kind"] == "stale_view"
    assert errs[0]["task"]["tool"] == "edit_file"


def test_teammate_failures_are_attributed_to_their_source(mgr):
    """A teammate failing is the Lead's business: actor records whose it was."""
    mgr.record("tool_done", actor="rev_2", tool="bash", name="bash", status="error",
               output="command not found: pytest", data={"args": {"command": "pytest"}})
    err = _head_execution(mgr)["last_error"]
    assert err["actor"] == "rev_2"
    assert err["reason"]["kind"] == "env"
    assert err["task"]["command"] == "pytest"


# --------------------------------------------------------------------------- #
# v2 end to end: "restore, then reply 继续" actually reaches the model
# --------------------------------------------------------------------------- #

def _request(text: str) -> str:
    """The user's own turn, after whatever prelude the agent prepended.

    The prelude is joined with a blank line and the request always comes last
    ("recent = strongest"), so the request is the tail.
    """
    return text.rsplit("\n\n", 1)[-1]


def _resumable_agent(tmp_path):
    """A real Agent that has finished one turn on a task worth resuming."""
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="好的，开始修")]),
                  tools=[], memory_enabled=False, checkpoint_enabled=True,
                  checkpoint_dir=str(tmp_path))
    agent.chat("修好登录 bug")
    return agent


def test_a_bare_continue_after_a_restore_reaches_the_model_as_the_task(tmp_path):
    """The point of the whole exercise: v1 handed back state and then waited for
    an instruction, so the recovered agent's first word was "要做什么？"."""
    agent = _resumable_agent(tmp_path)
    cp_id = agent.checkpoints.head_id()

    ok, _ = agent.restore_state(cp_id)
    assert ok

    agent.llm._turns = [LLMResponse(content="接着修")]
    agent.chat("继续")

    sent = agent.messages[-2]["content"]        # the user turn just sent
    assert _request(sent).strip() == "继续：修好登录 bug", sent


def test_the_log_keeps_the_raw_request_and_the_augmented_prompt_separately(tmp_path):
    """v1 logged only the augmented text -- which quotes old handoffs, so the
    "prefix accumulation" was already written into the log (review01 §1)."""
    agent = _resumable_agent(tmp_path)
    agent.restore_state(agent.checkpoints.head_id())
    agent.llm._turns = [LLMResponse(content="接着修")]
    agent.chat("继续")

    last = [e for e in agent.checkpoints.log.read() if e.type == "user_message"][-1]
    assert last.input == "继续"                       # what the user typed
    assert last.data["raw"] == "继续"
    assert "继续：修好登录 bug" in last.data["prompt"]
    # the resume text is built from the raw request, so it can never contain
    # the prefix it is about to add
    assert agent.checkpoints._resume_text == "修好登录 bug"


def test_restoring_and_continuing_twice_never_stacks_the_prefix(tmp_path):
    agent = _resumable_agent(tmp_path)
    cp_id = agent.checkpoints.head_id()

    agent.restore_state(cp_id)
    agent.llm._turns = [LLMResponse(content="一")]
    agent.chat("继续")
    agent.restore_state(cp_id)
    agent.llm._turns = [LLMResponse(content="二")]
    agent.chat("继续：继续")

    sent = _request(agent.messages[-2]["content"])
    assert sent.strip() == "继续：修好登录 bug"


def test_a_real_instruction_after_a_restore_is_not_rewritten(tmp_path):
    agent = _resumable_agent(tmp_path)
    agent.restore_state(agent.checkpoints.head_id())
    agent.llm._turns = [LLMResponse(content="好")]
    agent.chat("先别修了，改成加日志")

    sent = _request(agent.messages[-2]["content"])
    assert sent.strip() == "先别修了，改成加日志", "a real instruction outranks the pending task"


def test_the_handoff_rides_in_the_same_request_as_the_resumed_task(tmp_path):
    agent = _resumable_agent(tmp_path)
    agent.restore_state(agent.checkpoints.head_id())
    agent.llm._turns = [LLMResponse(content="好")]
    agent.chat("继续")

    sent = agent.messages[-2]["content"]
    assert "[断点恢复]" in sent, "the agent must re-orient before it works"
    assert sent.rstrip().endswith("继续：修好登录 bug"), "the task stays the last thing read"


# --------------------------------------------------------------------------- #
# v2: the approval path, end to end (bash confirm -> structured event)
# --------------------------------------------------------------------------- #

def test_bash_publishes_what_it_is_waiting_for(tmp_path):
    """The command and the reason come out *structured*, not scraped back out
    of the rendered sentence -- which is what v1 did, and why rewording the
    message used to silently empty the handoff's pending-approval line."""
    from encoder.tools.bash import NEEDS_CONFIRM, BashTool, take_pending_approval

    tool = BashTool()
    out = tool.execute("git push --force origin main")

    assert NEEDS_CONFIRM in out
    pending = take_pending_approval()
    assert pending == {"tool": "bash", "command": "git push --force origin main",
                       "reason": "force-push rewrites remote history"}
    # popped, not read: a later tool result in this thread must not inherit it
    assert take_pending_approval() is None


def test_a_confirming_command_leaves_nothing_pending():
    from encoder.tools.bash import BashTool, take_pending_approval

    tool = BashTool()
    tool.execute("echo hello")          # not risky at all
    assert take_pending_approval() is None


def test_a_refused_command_is_not_pending_but_is_an_error(tmp_path):
    """A teammate cannot confirm, so it is refused outright -- that one really
    did fail, and it must not be reported as "waiting for a human"."""
    from encoder.tools.bash import NEEDS_CONFIRM, BashTool, take_pending_approval

    tool = BashTool()
    tool.can_confirm = False
    out = tool.execute("git reset --hard HEAD~3")

    assert NEEDS_CONFIRM not in out and "Refused" in out
    assert take_pending_approval() is None


def _bash_that_needs_approval(tmp_path, command="git push --force"):
    """A real Agent whose real bash tool was asked to run a risky command.

    Driven through ``_exec_tool`` on purpose: the approval is published on the
    worker's own thread and popped when the result is logged, so a hand-made
    result string would test nothing (the command is never executed -- the
    confirm branch returns before ``_run``).
    """
    from encoder.llm import ToolCall
    from encoder.tools.bash import BashTool

    agent = Agent(llm=ScriptedLLM([]), tools=[BashTool()], memory_enabled=False,
                  checkpoint_enabled=True, checkpoint_dir=str(tmp_path))
    tc = ToolCall(id="c1", name="bash", arguments={"command": command})
    agent._record_tool(tc, agent._exec_tool(tc))
    return agent, tc


def test_the_recorded_approval_names_the_command_and_is_a_single_event(tmp_path):
    """One event, not two: v1 emitted tool_done *and* approval, so the pair
    differed only in meta and wrote two near-identical snapshots."""
    agent, _ = _bash_that_needs_approval(tmp_path)

    approvals = [e for e in agent.checkpoints.log.read() if e.type == "approval"]
    assert len(approvals) == 1
    assert approvals[0].data["command"] == "git push --force"
    assert approvals[0].data["reason"] == "force-push rewrites remote history"
    assert approvals[0].status == "pending"

    execu = agent.checkpoints.load(agent.checkpoints.head_id())["state"]["execution"]
    assert execu["pending_approval"]["command"] == "git push --force"
    assert execu["resume"]["kind"] == "approval"
    # and it is NOT in last_error: waiting for a human is not being stuck
    assert execu["last_error"] is None
    assert not execu["errors"]


def test_the_handoff_tells_the_agent_what_to_ask_about(tmp_path):
    agent, _ = _bash_that_needs_approval(tmp_path)

    restored = agent.checkpoints.restore(agent.checkpoints.head_id())
    assert "git push --force" in restored.handoff
    assert "上一次在等用户批准这条命令" in restored.handoff
    assert "上次为什么停下" not in restored.handoff, "waiting is not being stuck"


# --------------------------------------------------------------------------- #
# ⑤ long-running calls: a marker written *before* the call, not after it
# --------------------------------------------------------------------------- #

def test_looks_long_reads_the_models_own_declaration_and_the_command():
    """Two signals, and the timeout one matters most: it is the model saying
    "this may take a while", it costs nothing to read, and unlike a command list
    it cannot rot. The list is only a floor under it."""
    from encoder.checkpoint import looks_long

    cases = [
        # the model declared it
        ("bash", {"command": "ls", "timeout": 300}, True),
        ("bash", {"command": "ls", "timeout": "120"}, True),
        ("bash", {"command": "ls", "timeout": 30}, False),
        # slow by nature, matched on whole words -- "echo make" is not a build
        ("bash", {"command": "pip install -e ."}, True),
        ("bash", {"command": "cd /tmp && npm ci"}, True),
        ("bash", {"command": "git status; pytest -q"}, True),
        ("bash", {"command": "sudo apt-get install -y curl"}, True),
        ("bash", {"command": "echo make"}, False),
        ("bash", {"command": "grep -rn 'make' src/"}, False),
        ("bash", {"command": "ls"}, False),
        ("bash", {"command": None}, False),
        # only bash: every other tool returns promptly, so a marker would be noise
        ("read_file", {"file_path": "a.py", "timeout": 600}, False),
        ("edit_file", {"command": "pip install x"}, False),
    ]
    for tool, args, want in cases:
        assert looks_long(tool, args) is want, (tool, args)


def test_a_long_marker_is_a_hard_and_a_milestone_kind():
    """Hard, because the point is the snapshot that exists when the call does not
    come back -- throttling it away would defeat the whole trigger. A milestone,
    because the model declared the boundary ("this runs long")."""
    assert "long" in HARD_TRIGGERS
    assert "long" in MILESTONE_TRIGGERS
    assert trigger_for(Event(type="tool_start", data={"long": True})) == "long"
    # the policy reads the flag instead of assuming the emitter only logs long calls
    assert trigger_for(Event(type="tool_start")) is None


def _agent_with_bash(tmp_path, tool=None):
    from encoder.tools.bash import BashTool

    return Agent(llm=ScriptedLLM([]), tools=[tool or BashTool()],
                 memory_enabled=False, checkpoint_enabled=True,
                 checkpoint_dir=str(tmp_path))


def test_a_long_call_is_marked_before_it_runs(tmp_path):
    """The ordering is the whole feature: the marker's seq has to be *below* the
    result's, because a call killed halfway never writes the result."""
    from encoder.llm import ToolCall

    agent = _agent_with_bash(tmp_path)
    tc = ToolCall(id="c1", name="bash",
                  arguments={"command": "echo hi", "timeout": 300})
    agent._record_tool(tc, agent._exec_tool(tc))

    events = agent.checkpoints.log.read()
    starts = [e for e in events if e.type == "tool_start"]
    dones = [e for e in events if e.type == "tool_done"]
    assert len(starts) == 1 and len(dones) == 1
    assert starts[0].data["long"] is True
    assert starts[0].data["args"] == {"command": "echo hi", "timeout": 300}
    assert starts[0].seq < dones[0].seq

    long_cps = [m for m in agent.checkpoints.list_checkpoints() if m.get("trigger") == "long"]
    assert len(long_cps) == 1
    assert long_cps[0]["label"] == "长耗时调用前"
    assert long_cps[0]["seq"] <= starts[0].seq < dones[0].seq


def test_an_ordinary_call_is_not_marked(tmp_path):
    """⑤ is not "snapshot before every tool call" -- that is the 无脑保存 the
    design warns about. A plain echo leaves the log exactly as it was."""
    from encoder.llm import ToolCall

    agent = _agent_with_bash(tmp_path)
    tc = ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})
    agent._record_tool(tc, agent._exec_tool(tc))

    assert not [e for e in agent.checkpoints.log.read() if e.type == "tool_start"]
    assert not [m for m in agent.checkpoints.list_checkpoints() if m.get("trigger") == "long"]


class _BashThatDiesMidCall:
    """A bash tool that dies where a SIGKILL would: inside the call.

    Not a fake result string -- ``_exec_tool`` is driven for real, so the marker
    is emitted by the production path and the ``tool_done`` that follows it in
    every other test simply never happens.
    """

    def __init__(self):
        from encoder.tools.bash import BashTool
        self._inner = BashTool()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, **kwargs) -> str:
        raise KeyboardInterrupt


def _agent_that_died_inside_a_long_call(tmp_path):
    from encoder.llm import LLMResponse, ToolCall

    agent = _agent_with_bash(tmp_path, tool=_BashThatDiesMidCall())
    tc = ToolCall(id="c1", name="bash", arguments={"command": "pip install -e ."})
    # exactly what the loop does before executing: the assistant turn is in the
    # history, and its tool_call never gets a reply
    agent.messages.append(LLMResponse(content="", tool_calls=[tc]).message)
    with pytest.raises(KeyboardInterrupt):
        agent._exec_tool(tc)
    return agent


def test_a_process_that_dies_inside_a_long_call_still_says_what_was_running(tmp_path):
    """The case nothing else covers: the call's result never arrives, so the
    only record of what was in flight is the marker written before it started."""
    agent = _agent_that_died_inside_a_long_call(tmp_path)

    events = agent.checkpoints.log.read()
    assert [e for e in events if e.type == "tool_start"], "no marker was written"
    assert not [e for e in events if e.type == "tool_done"], "the call returned?"

    restored = agent.checkpoints.restore(agent.checkpoints.head_id())
    assert restored.resume["kind"] == "tool"
    assert "pip install -e ." in restored.handoff
    # and the one thing the prompt must NOT invite is a blind re-run
    assert "副作用" in restored.handoff
    assert "重跑" in restored.handoff


def test_the_unanswered_call_is_named_once_with_one_explanation(tmp_path):
    """待续 and 上次中断 are both fed by the same pending call, so both used to
    print it -- and once the long-call reason existed, the two copies explained
    the *same* call differently. One owner (``_why_unanswered``), one printing."""
    agent = _agent_that_died_inside_a_long_call(tmp_path)
    handoff = agent.checkpoints.restore(agent.checkpoints.head_id()).handoff

    assert handoff.count("pip install -e .") == 1
    assert "■ 上次中断" not in handoff
    assert handoff.count("副作用") == 1


def test_an_approval_keeps_the_do_not_rerun_instruction(tmp_path):
    """Removing the duplicate block must not remove what it was there to say."""
    agent, _ = _bash_that_needs_approval(tmp_path)
    handoff = agent.checkpoints.restore(agent.checkpoints.head_id()).handoff

    assert handoff.count("git push --force") == 1
    assert "先问用户，不要自己重跑" in handoff
