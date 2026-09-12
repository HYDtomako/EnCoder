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
from encoder.llm import ScriptedLLM
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

    restored = mgr.restore(first)

    # the state handed back is the checkpoint that was named
    assert restored.messages == [{"role": "user", "content": "one"}]
    for cid, text in before.items():
        assert mgr.dir.joinpath(f"{cid}.json").read_text(encoding="utf-8") == text
    # the rewind is recorded as a new event, and as a new recovery point whose
    # parent is the checkpoint it came from -- so a restore is itself undoable
    assert any(e.type == "rewind" for e in mgr.log.read())
    assert mgr.head_id() not in (first, second)
    assert mgr.load(mgr.head_id())["meta"]["parent_id"] == second


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
