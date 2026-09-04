"""Tests for the multi-agent team (add3.0 / DESIGN_agent_team_v1.md).

Covers the Mailbox (persisted per-agent inboxes + priority), the TeamManager
(spawn/collect/review/release + the teammate cap and one-task-one-teammate
rule), the deterministic result summary, and the team tools' off-by-default
hint. Teammates run real threads but against a fake offline LLM.
"""

import os
import subprocess
import time

import pytest

from encoder.context import ContextManager
from encoder.llm import LLMResponse
from encoder.task import TaskManager
from encoder.team import TEAM_MAX, Mailbox, TeamManager, Teammate
from encoder.tools import ALL_TOOLS
from encoder.tools.paths import resolve, set_cwd, set_root


# --------------------------------------------------------------------------- #
# fixtures / fakes
# --------------------------------------------------------------------------- #

class _FakeLLM:
    """Deterministic offline LLM: always replies with the same text, no tools."""
    total_prompt_tokens = 0
    total_completion_tokens = 0
    model = "test"

    def __init__(self, content: str = "teammate done"):
        self.content = content

    def chat(self, messages, tools=None, on_token=None):
        return LLMResponse(content=self.content)


class _FakeLead:
    """Stand-in for Agent: exposes the bits TeamManager.spawn reads off the Lead."""
    def __init__(self, base_dir, llm=None):
        self.tasks = TaskManager(base_dir=base_dir)
        self.llm = llm or _FakeLLM()
        self.context = ContextManager(max_tokens=128_000)
        self.max_rounds = 50
        self.tools = ALL_TOOLS
        self.team = None


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _team(tmp_path, lead=None):
    lead = lead or _FakeLead(tmp_path)
    return TeamManager(lead=lead, base_dir=tmp_path / "mailbox"), lead


# --------------------------------------------------------------------------- #
# Mailbox
# --------------------------------------------------------------------------- #

def test_mailbox_post_pop_roundtrip(tmp_path):
    mb = Mailbox(base_dir=tmp_path)
    mb.post("agent_1", {"from": "Lead", "kind": "task", "content": "do it"})
    msg = mb.pop("agent_1")
    assert msg["kind"] == "task"
    assert msg["content"] == "do it"
    assert mb.pop("agent_1", timeout=0.1) is None   # consumed exactly once


def test_mailbox_review_beats_queued_task(tmp_path):
    mb = Mailbox(base_dir=tmp_path)
    mb.post("agent_1", {"from": "Lead", "kind": "task", "content": "new task"})
    mb.post("agent_1", {"from": "Lead", "kind": "review", "content": "fix it"})
    assert mb.pop("agent_1")["kind"] == "review"
    assert mb.pop("agent_1")["kind"] == "task"


def test_mailbox_end_beats_everything(tmp_path):
    mb = Mailbox(base_dir=tmp_path)
    mb.post("agent_1", {"from": "Lead", "kind": "task", "content": "x"})
    mb.post("agent_1", {"from": "Lead", "kind": "review", "content": "y"})
    mb.post("agent_1", {"from": "Lead", "kind": "end", "content": ""})
    assert mb.pop("agent_1")["kind"] == "end"


def test_mailbox_drain_reads_and_clears(tmp_path):
    mb = Mailbox(base_dir=tmp_path)
    mb.post("Lead", {"from": "agent_1", "kind": "result", "content": "r1"})
    mb.post("Lead", {"from": "agent_2", "kind": "result", "content": "r2"})
    msgs = mb.drain("Lead")
    assert [m["content"] for m in msgs] == ["r1", "r2"]
    assert mb.drain("Lead") == []                        # cleared
    assert (tmp_path / "Lead.json").exists()


def test_mailbox_broadcast_excludes_sender(tmp_path):
    mb = Mailbox(base_dir=tmp_path)
    mb.register("Lead")
    mb.register("agent_1")
    mb.register("agent_2")
    targets = mb.broadcast("broken dep", from_="Lead")
    assert sorted(targets) == ["agent_1", "agent_2"]
    a1 = mb.pop("agent_1")
    a2 = mb.pop("agent_2")
    assert a1["kind"] == "notice" and a2["kind"] == "notice"
    assert a1["content"] == "broken dep"


# --------------------------------------------------------------------------- #
# render_summary - deterministic "队友结果摘要"
# --------------------------------------------------------------------------- #

def test_render_summary_deterministic():
    results = [
        {"from": "agent_1", "kind": "result", "task_id": "t1", "content": "done A"},
    ]
    statuses = [{"name": "agent_1", "status": "idle", "task_id": "t1"}]
    out = TeamManager.render_summary(None, results, statuses)
    assert out.startswith("# 队友结果摘要")
    assert "[agent_1] task t1 → done A" in out
    assert "agent_1 idle" in out


def test_render_summary_empty_when_nothing():
    assert TeamManager.render_summary(None, [], []) == ""


# --------------------------------------------------------------------------- #
# TeamManager spawn / collect / release
# --------------------------------------------------------------------------- #

def test_team_off_by_default():
    # a plain Agent has no team manager unless team_enabled was passed
    from encoder import Agent, LLM
    agent = Agent(llm=LLM.__new__(LLM), tools=[], memory_enabled=False)
    assert agent.team is None
    assert agent.team_enabled is False


def test_spawn_runs_teammate_and_collects_result(tmp_path):
    team, lead = _team(tmp_path)
    teammate = team.spawn(description="写一个 hello world")
    tid = teammate.current_task_id
    assert teammate.name == "agent_1"
    # wait for the teammate thread to finish and mark the task completed
    assert _wait_for(lambda: lead.tasks.get(tid).state == "completed")

    results = team.collect()
    assert results, "teammate should have posted a result to Lead"
    r = results[0]
    assert r["kind"] == "result"
    assert r["from"] == "agent_1"
    assert "teammate done" in r["content"]
    assert lead.tasks.get(tid).result == "teammate done"
    team.release_all()


def test_spawn_persists_task_review_end_messages(tmp_path):
    """All message kinds are written into .Mailbox/<name>.json (user-confirmed)."""
    team, lead = _team(tmp_path)
    teammate = team.spawn(description="持久化消息")
    tid = teammate.current_task_id
    assert _wait_for(lambda: lead.tasks.get(tid).state == "completed")
    # a task message was persisted to the teammate's inbox file
    mb_dir = tmp_path / "mailbox"
    assert (mb_dir / f"{teammate.name}.json").exists()
    team.review(teammate.name, "再改一下")
    team.release(teammate.name)
    # the end message is persisted too
    assert (mb_dir / f"{teammate.name}.json").exists()


def test_teammate_limit_reached(tmp_path):
    team, lead = _team(tmp_path)
    spawned = [team.spawn(description=f"task {i}") for i in range(TEAM_MAX)]
    with pytest.raises(RuntimeError) as e:
        team.spawn(description="one too many")
    assert "limit" in str(e.value)
    for t in spawned:
        team.release(t.name)


def test_one_task_one_teammate(tmp_path):
    team, lead = _team(tmp_path)
    task = lead.tasks.create("只能一个队友")
    team.spawn(task_id=task.task_id)
    with pytest.raises(RuntimeError) as e:
        team.spawn(task_id=task.task_id)
    assert "already handled" in str(e.value)
    team.release_all()


def test_spawn_rejects_unknown_task(tmp_path):
    team, _ = _team(tmp_path)
    with pytest.raises(KeyError):
        team.spawn(task_id="nope")


def test_release_ends_teammate(tmp_path):
    team, lead = _team(tmp_path)
    teammate = team.spawn(description="会被释放")
    tid = teammate.current_task_id
    assert _wait_for(lambda: lead.tasks.get(tid).state == "completed")
    assert team.release(teammate.name) is True
    assert _wait_for(lambda: teammate.status == "ending")
    assert team.release("ghost") is False


def test_review_queues_and_prioritises(tmp_path):
    team, lead = _team(tmp_path)
    teammate = team.spawn(description="review 目标")
    tid = teammate.current_task_id
    assert _wait_for(lambda: lead.tasks.get(tid).state == "completed")
    assert team.review(teammate.name, "请用中文再回答一次") is True
    # a review is delivered to the teammate's inbox (priority over any new task)
    assert _wait_for(lambda: teammate.status == "idle")
    assert team.review("ghost", "x") is False
    team.release_all()


# --------------------------------------------------------------------------- #
# team tools - off-by-default hint
# --------------------------------------------------------------------------- #

def test_team_tools_return_hint_when_off():
    from encoder.tools.team import (
        CollectResultsTool, SpawnTeammateTool, ReviewTeammateTool,
        ReleaseTeammateTool, BroadcastNoticeTool,
    )

    class _NoTeam:
        team = None

    parent = _NoTeam()
    for cls in (SpawnTeammateTool, CollectResultsTool, ReviewTeammateTool,
                ReleaseTeammateTool, BroadcastNoticeTool):
        tool = cls()
        tool._parent_agent = parent
        if cls is SpawnTeammateTool:
            out = tool.execute(description="x")
        elif cls is ReviewTeammateTool:
            out = tool.execute(name="agent_1", feedback="x")
        elif cls is ReleaseTeammateTool:
            out = tool.execute(name="agent_1")
        elif cls is BroadcastNoticeTool:
            out = tool.execute(content="x")
        else:
            out = tool.execute()
        assert "team" in out.lower() and "off" in out.lower(), (cls.__name__, out)


# --------------------------------------------------------------------------- #
# paths - thread-local cwd + worktree confinement
# --------------------------------------------------------------------------- #

def test_resolve_confines_to_root(tmp_path):
    set_cwd(str(tmp_path))
    set_root(str(tmp_path))
    try:
        assert resolve("sub/file.txt") == (tmp_path / "sub" / "file.txt").resolve()
        with pytest.raises(ValueError):
            resolve(str(tmp_path.parent / "escape.txt"))
    finally:
        set_cwd(None)
        set_root(None)


def test_resolve_uses_thread_local_cwd(tmp_path):
    set_cwd(str(tmp_path))
    try:
        assert resolve("relative.txt") == (tmp_path / "relative.txt").resolve()
    finally:
        set_cwd(None)


def test_bash_cd_refuses_escape_from_root(tmp_path):
    """A `cd` outside the worktree root is refused (cwd unchanged)."""
    import encoder.tools.bash as bash_mod

    set_root(str(tmp_path))
    try:
        bash_mod._local.cwd = None
        bash_mod._update_cwd(f"cd {tmp_path.parent}", str(tmp_path))
        assert getattr(bash_mod._local, "cwd", None) is None   # refused
        bash_mod._update_cwd(f"cd {tmp_path}", str(tmp_path))
        assert getattr(bash_mod._local, "cwd", None) == os.path.normpath(str(tmp_path))
    finally:
        set_root(None)
        bash_mod._local.cwd = None


# --------------------------------------------------------------------------- #
# worktree merge / integrate closed loop (review.md Item 1)
# --------------------------------------------------------------------------- #
# Every test below chdirs into a throwaway git repo so no git worktree is ever
# created inside the real project checkout.

def _git(repo, *args) -> str:
    cp = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A tiny throwaway git repo, and the process chdirs into it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@encoder")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    monkeypatch.chdir(repo)
    return repo


def _wt_teammate(team, lead, name, files: dict[str, str], seed: str) -> Teammate:
    """Create an already-finished (ending) worktree teammate with a committed edit."""
    wt, branch = team._setup_worktree(name)
    assert wt is not None, "worktree should be creatable inside the throwaway repo"
    for rel, text in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{name}: {seed}")
    t = Teammate(name, None, team.mailbox, lead.tasks, worktree=wt, branch=branch)
    t.status = "ending"                    # released, so integrate will merge it
    team._teammates[name] = t
    return t


def test_integrate_merges_disjoint_branches_and_cleans(git_repo):
    team, lead = _team(git_repo)
    team.worktrees = True
    _wt_teammate(team, lead, "agent_1", {"a.txt": "A one\n"}, "feature-a")
    _wt_teammate(team, lead, "agent_2", {"b.txt": "B two\n"}, "feature-b")

    out = team.integrate()

    # both edits are now on the current branch working tree
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "A one\n"
    assert (git_repo / "b.txt").read_text(encoding="utf-8") == "B two\n"
    assert "merged cleanly" in out
    # teammates dropped, branches gone, no leftover teammate worktrees
    assert team.status() == []
    assert _git(git_repo, "branch", "--list", "teammate/*") == ""
    wt_entries = _git(git_repo, "worktree", "list")
    assert ".worktrees" not in wt_entries


def test_integrate_conflict_kept_for_manual_when_integrator_cannot_finish(git_repo):
    """Add/add conflict: FakeLLM 'integrator' can't edit, so the merge aborts and
    the teammate/worktree are kept for manual handling instead of being lost."""
    team, lead = _team(git_repo)
    team.worktrees = True
    _wt_teammate(team, lead, "agent_1", {"c.txt": "one\n"}, "c1")
    _wt_teammate(team, lead, "agent_2", {"c.txt": "two\n"}, "c2")

    out = team.integrate()

    # agent_1 merged cleanly; agent_2's add/add conflict was NOT auto-resolved
    assert "conflict NOT resolved" in out
    # repo returned to a clean state (HEAD content), no in-progress merge
    no_merge = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                              cwd=git_repo, capture_output=True)
    assert no_merge.returncode != 0            # MERGE_HEAD must not exist
    assert (git_repo / "c.txt").read_text(encoding="utf-8") == "one\n"
    # agent_2 retained (branch + worktree) for the Lead to handle manually
    names = [s["name"] for s in team.status()]
    assert "agent_2" in names
    assert "agent_1" not in names
    assert _git(git_repo, "branch", "--list", "teammate/agent_2*") != ""


def test_integrate_no_released_teammates_returns_hint(git_repo):
    team, _ = _team(git_repo)
    out = team.integrate()
    assert "no released code teammates" in out


def test_worktree_creation_failure_is_loud_not_silent(tmp_path, monkeypatch):
    """When isolation is requested but impossible (not a git repo), spawn must
    say so instead of silently degrading back into the main working directory."""
    monkeypatch.chdir(tmp_path)              # tmp_path is NOT a git repo
    (tmp_path / "loose.txt").write_text("hi\n", encoding="utf-8")
    team, lead = _team(tmp_path)
    team.worktrees = True

    t = team.spawn(description="research only")
    assert t._worktree is None
    assert t._wt_note is not None
    assert "WITHOUT isolation" in t._wt_note

    team.release(t.name)
    assert _wait_for(lambda: t.status == "ending")
