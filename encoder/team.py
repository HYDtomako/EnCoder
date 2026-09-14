"""Multi-agent team: Lead + persistent Agent_teammates (add3.0 / DESIGN_agent_team_v1).

A teammate is NOT a subagent. A subagent runs one task and ends; a teammate runs
a task, reports the result to the Lead through a shared Mailbox, then stays IDLE
waiting for another task or a review - it only shuts down (``ending``) when the
Lead explicitly sends ``end``.

Concepts:

- ``Mailbox``: one inbox file per agent (``.Mailbox/<name>.json``). All five
  message kinds (task/review/end/result/notice) are persisted there; an in-memory
  priority queue provides blocking receive plus ``end < review < task < notice``
  ordering (a review always beats a queued new task).
- ``Teammate``: wraps an ``Agent`` with its own context/loop/thread; state
  ``idle -> work -> idle``, then ``ending`` only on the Lead's ``end``.
- ``TeamManager``: owns the teammate map under a lock, spawns/reviews/releases
  teammates, drains the Lead inbox, and renders a deterministic result summary.

Guarantees:

- One task = one teammate (never two agents on the same task).
- At most ``TEAM_MAX`` concurrent teammates (bounded so results back to the Lead
  stay manageable).
- A teammate failure never propagates: exceptions become a result message to the
  Lead plus the task's ``last_error``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .tools.paths import set_cwd, set_root

TEAM_MAX = 3   # concurrent teammate cap (user-confirmed)

# message-kind -> priority (lower dequeues first): end < review < task < notice
_KIND_PRIORITY = {"end": 0, "review": 1, "task": 2, "notice": 3}
_STATUSES = ("idle", "work", "ending")

# tools a teammate must NOT carry: managing the team itself, or spawning yet
# another agent (user-confirmed: no endless nesting). ``broadcast_notice`` stays.
_TEAMMATE_EXCLUDE = frozenset({
    "spawn_teammate", "collect_results", "review_teammate", "release_teammate",
    "agent", "dispatch_task",
})

_RESULT_TRIM = 500   # per-result chars shown in the Lead's summary


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_msg_id() -> str:
    return "m" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# Mailbox - one inbox per agent
# --------------------------------------------------------------------------- #

class Mailbox:
    """Per-agent inboxes: ``.Mailbox/<name>.json`` (truth) + PriorityQueue (wakeup/order)."""

    def __init__(self, base_dir: Path = Path(".Mailbox")):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self._queues: dict[str, "queue.PriorityQueue"] = {}
        self._seq = 0
        self._inboxes: set[str] = set()

    # -- file helpers ---------------------------------------------------------

    def _path(self, who: str) -> Path:
        return self.base_dir / f"{who}.json"

    def _queue_of(self, who: str) -> "queue.PriorityQueue":
        q = self._queues.get(who)
        if q is None:
            q = queue.PriorityQueue()
            self._queues[who] = q
        return q

    def _read(self, who: str) -> list[dict]:
        p = self._path(who)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, who: str, msgs: list[dict]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.base_dir / f"{who}.json.tmp"
        tmp.write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path(who))

    # -- public API -----------------------------------------------------------

    def register(self, who: str) -> None:
        """Ensure an inbox exists for ``who`` so broadcast reaches it."""
        with self._lock:
            self._inboxes.add(who)

    def post(self, to: str, msg: dict) -> None:
        """Persist ``msg`` to ``<to>.json`` and wake the receiver's queue."""
        msg = dict(msg)
        msg.setdefault("msg_id", _new_msg_id())
        msg.setdefault("to", to)
        msg.setdefault("ts", _now())
        with self._lock:
            self._seq += 1
            msgs = self._read(to)
            msgs.append(msg)
            self._write(to, msgs)
            prio = _KIND_PRIORITY.get(msg.get("kind"), _KIND_PRIORITY["task"])
            self._queue_of(to).put((prio, self._seq, msg["msg_id"], msg))
            self._inboxes.add(to)
            if msg.get("from"):
                self._inboxes.add(msg["from"])

    def pop(self, who: str, timeout: float | None = None) -> dict | None:
        """Blocking receive of the highest-priority message; consumes it."""
        try:
            _prio, _seq, msg_id, msg = self._queue_of(who).get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            self._write(who, [m for m in self._read(who) if m.get("msg_id") != msg_id])
        return msg

    def drain(self, who: str) -> list[dict]:
        """Read-and-clear all of ``who``'s messages, priority-sorted (non-blocking)."""
        with self._lock:
            msgs = self._read(who)
            if msgs:
                self._write(who, [])
        q = self._queues.get(who)
        if q is not None:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        msgs.sort(key=lambda m: (_KIND_PRIORITY.get(m.get("kind"), 2), m.get("ts", "")))
        return msgs

    def broadcast(self, content: str, from_: str, exclude: set[str] | None = None) -> list[str]:
        """Post a ``notice`` to every known inbox except ``from_``/``exclude``."""
        exclude = set(exclude or ()) | {from_}
        with self._lock:
            targets = [n for n in sorted(self._inboxes) if n not in exclude]
        for name in targets:
            self.post(name, {"from": from_, "kind": "notice", "content": content})
        return targets


# --------------------------------------------------------------------------- #
# Teammate - a persistent worker on its own thread
# --------------------------------------------------------------------------- #

class Teammate:
    """A persistent worker: its own ``Agent`` on its own thread, work/idle/ending."""

    def __init__(self, name: str, agent, mailbox: Mailbox, tasks,
                 worktree: Path | None = None, branch: str | None = None,
                 on_event=None):
        self.name = name
        self.agent = agent
        self.mailbox = mailbox
        self.tasks = tasks                      # the Lead's TaskManager
        self.on_event = on_event                # report status hops to the log
        self.status = "idle"
        self.thread: threading.Thread | None = None
        self.current_task_id: str | None = None
        self._worktree = str(worktree) if worktree else None
        self._branch = branch                   # teammate/<name>_<suffix>
        self._wt_note = None                    # isolation failure reason (spawn sets it)
        self._notices: list[str] = []           # broadcasts held for the next task

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self.thread.start()

    def _set_status(self, status: str) -> None:
        """Move the state machine and report the hop (trace §3.5).

        The teammate's two *meaningful* hops -- picking work up and putting it
        down -- had no event at all: the Lead's log showed a teammate being
        spawned and released, and nothing in between. `ending` is not repeated
        here; the release event already says it.
        """
        previous, self.status = self.status, status
        if self.on_event is not None:
            self.on_event("status", name=self.name, status=status,
                          previous=previous, to=status,
                          task_id=self.current_task_id or "",
                          branch=self._branch or "", worktree=self._worktree or "")

    def _run(self) -> None:
        if self._worktree:
            set_cwd(self._worktree)
            set_root(self._worktree)            # cd/read/write must not escape the root
        while True:
            msg = self.mailbox.pop(self.name)
            if msg is None:
                continue
            kind = msg.get("kind")

            if kind == "end":                   # only the Lead's end shuts us down
                self.status = "ending"
                return
            if kind == "notice":                # hold broadcasts, inject on next task
                self._notices.append(msg.get("content", ""))
                continue

            if kind == "task":
                self.current_task_id = msg.get("task_id")
                prompt = self._task_prompt(msg)
                is_task = True
            else:                               # review (already prioritised over task)
                prompt = msg.get("content", "")
                is_task = False

            self._set_status("work")
            try:
                result = self.agent.chat(prompt)
            except Exception as e:
                result = f"[teammate error] {e}"
            # commit any repo edits onto our own branch *while still work*, so an
            # integrate() merge (which only touches idle/ending teammates) never
            # races a mid-commit worktree. (This is a git commit -- unrelated to
            # the agent-level checkpoint in checkpoint.py.)
            try:
                self._commit_work()
            finally:
                self._set_status("idle")

            if is_task:
                self._report_task(self.current_task_id, result)
            else:
                self.mailbox.post("Lead", {
                    "from": self.name, "kind": "result",
                    "task_id": self.current_task_id, "content": result,
                })
            self.current_task_id = None

    def _commit_work(self) -> None:
        """Deterministically commit this worktree teammate's edits onto its branch.

        No-op when the teammate has no worktree (ran directly in the main cwd)
        or when nothing changed. Best-effort: a commit failure must never break
        the teammate loop -- the branch just stays ahead-of/main via git.
        """
        if not (self._worktree and self._branch):
            return
        try:
            changed = subprocess.run(
                ["git", "-C", self._worktree, "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            ).stdout.strip()
            if not changed:
                return
            subprocess.run(["git", "-C", self._worktree, "add", "-A"],
                           check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", self._worktree, "commit", "-m",
                 f"teammate {self.name}: work result"],
                check=True, capture_output=True, text=True,
            )
        except Exception:
            pass  # committing is best-effort

    def _task_prompt(self, msg: dict) -> str:
        content = msg.get("content", "")
        if self._notices:
            block = "\n".join(f"- {n}" for n in self._notices)
            self._notices.clear()
            return f"[队友广播，仅供参考]\n{block}\n\n{content}"
        return content

    def _report_task(self, task_id: str | None, result: str) -> None:
        if task_id:
            try:
                self.tasks.mark_completed(task_id, result)
            except Exception as e:
                result = f"{result}\n[mark_completed error: {e}]"
        self.mailbox.post("Lead", {
            "from": self.name, "kind": "result",
            "task_id": task_id, "content": result,
        })


# --------------------------------------------------------------------------- #
# TeamManager - owns the team
# --------------------------------------------------------------------------- #

class TeamManager:
    """Owns the teammate map; spawns/reviews/releases teammates under a lock."""

    def __init__(self, lead, base_dir: Path = Path(".Mailbox"),
                 worktrees: bool = False, max_teammates: int = TEAM_MAX,
                 team_model: str | None = None,
                 team_api_key: str | None = None,
                 team_base_url: str | None = None,
                 integration_model: str | None = None,
                 integration_api_key: str | None = None,
                 integration_base_url: str | None = None):
        self.lead = lead
        self.mailbox = Mailbox(base_dir)
        self.mailbox.register("Lead")
        self._teammates: dict[str, Teammate] = {}
        self._counter = 0
        self._lock = threading.Lock()           # add3.0's thread_lock
        self.worktrees = worktrees
        self.max_teammates = max_teammates
        # optional dedicated teammate model; falls back to the Lead's LLM
        self.team_model = team_model
        self.team_api_key = team_api_key
        self.team_base_url = team_base_url
        self._teammate_llm = None
        # optional dedicated Integration-Agent model (better model for merge
        # conflict reconciliation); falls back to the teammate/Lead LLM chain
        self.integration_model = integration_model
        self.integration_api_key = integration_api_key
        self.integration_base_url = integration_base_url
        self._integration_llm = None
        # observation hook for the checkpoint layer: on_event(action, name, **fields).
        # A teammate's worktree/branch/status live only in this object, so a
        # snapshot could not otherwise know a teammate exists at all.
        self.on_event = None

    # -- helpers --------------------------------------------------------------

    def _notify(self, action: str, name: str = "", **fields) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(action, name=name, **fields)
        except Exception:
            pass          # observation must never break the team

    def _active(self) -> list[Teammate]:
        return [t for t in self._teammates.values() if t.status != "ending"]

    def _next_name(self) -> str:
        while True:
            self._counter += 1
            name = f"agent_{self._counter}"
            if name not in self._teammates:
                return name

    def _teammate_llm_for(self):
        """Lazy dedicated teammate LLM if a team model is configured, else Lead's.

        Follows the memory min-LLM pattern: only build a separate client when
        ``ENCODER_TEAM_MODEL`` (or team_model) is set and differs from the
        Lead's model; otherwise every teammate shares the Lead's LLM instance.
        """
        if self._teammate_llm is not None:
            return self._teammate_llm
        lead_llm = self.lead.llm
        model = self.team_model
        if model and model != getattr(lead_llm, "model", model):
            from .llm import LLM              # local import: avoid circular import
            api_key = (self.team_api_key
                       or os.getenv("ENCODER_API_KEY")
                       or os.getenv("OPENAI_API_KEY")
                       or os.getenv("DEEPSEEK_API_KEY") or "")
            base_url = (self.team_base_url
                        or os.getenv("OPENAI_BASE_URL")
                        or os.getenv("ENCODER_BASE_URL"))
            self._teammate_llm = LLM(model=model, api_key=api_key, base_url=base_url)
        else:
            self._teammate_llm = lead_llm
        return self._teammate_llm

    def _integration_llm_for(self):
        """LLM for the one-shot Integration Agent (merge-conflict reconciler).

        Uses ``ENCODER_INTEGRATION_MODEL`` when configured (a deliberately
        better model for this role), otherwise the Lead's own LLM.
        """
        if self._integration_llm is not None:
            return self._integration_llm
        lead_llm = self.lead.llm
        model = self.integration_model
        if model and model != getattr(lead_llm, "model", model):
            from .llm import LLM              # local import: avoid circular import
            api_key = (self.integration_api_key
                       or os.getenv("ENCODER_API_KEY")
                       or os.getenv("OPENAI_API_KEY")
                       or os.getenv("DEEPSEEK_API_KEY") or "")
            base_url = (self.integration_base_url
                        or os.getenv("OPENAI_BASE_URL")
                        or os.getenv("ENCODER_BASE_URL"))
            self._integration_llm = LLM(model=model, api_key=api_key, base_url=base_url)
        else:
            self._integration_llm = lead_llm
        return self._integration_llm

    def _build_agent(self, tools):
        from .agent import Agent              # local import: avoid circular import
        from .tools.bash import disable_confirmation  # local import: ditto
        agent = Agent(
            llm=self._teammate_llm_for(),
            tools=tools,
            max_context_tokens=self.lead.context.max_tokens,
            max_rounds=self.lead.max_rounds,
            memory_enabled=False,             # teammates' own context is enough
        )
        # an unattended teammate has no user who could approve a high-risk
        # command, so its bash can never self-confirm (review.md Item 2)
        disable_confirmation(agent.tools)
        return agent

    def _git(self, args, cwd=None) -> subprocess.CompletedProcess:
        """Run a native git command (never through the bash tool)."""
        try:
            return subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return type("CP", (), {
                "returncode": 127, "stdout": "", "stderr": "git: command not found",
            })()

    def _setup_worktree(self, name: str):
        """Create an isolated worktree+branch for a teammate.

        Returns ``(worktree_path, branch)`` on success, or ``None`` on failure
        (caller must surface it -- isolation must never degrade silently).
        A fresh ``teammate/<name>_<suffix>`` branch + ``.worktrees/<name>_<suffix>``
        dir is created per spawn so a stale registration from a crashed session
        cannot block the next spawn.
        """
        self._git(["worktree", "prune"])       # drop stale registrations
        suffix = uuid.uuid4().hex[:6]
        branch = f"teammate/{name}_{suffix}"
        wt = Path(".worktrees") / f"{name}_{suffix}"
        cp = self._git(["worktree", "add", "-b", branch, str(wt)])
        if cp.returncode != 0:
            return None
        return wt, branch

    # -- spawn ----------------------------------------------------------------

    def spawn(self, task_id: str | None = None, description: str | None = None,
              worktree: bool | None = None) -> Teammate:
        """Assign a task to a new persistent teammate and start it in parallel.

        ``worktree``: None = follow the manager default (``worktrees``), which
        the CLI turns on by default so code-editing teammates are isolated.
        Pass ``False`` for research / read-only tasks that won't touch the repo.
        """
        from .tools.agent import clone_tools  # local import: avoid circular import

        with self._lock:
            if len(self._active()) >= self.max_teammates:
                raise RuntimeError(
                    f"teammate limit reached ({self.max_teammates}); release one first")

            tasks = self.lead.tasks
            if task_id:
                task = tasks.get(task_id)
                if task is None:
                    raise KeyError(f"no such task '{task_id}'")
            elif description:
                task = tasks.create(str(description).strip(), agent="")
            else:
                raise ValueError("provide task_id or description")

            # one task = one teammate (point 9)
            for t in self._teammates.values():
                if t.status != "ending" and t.current_task_id == task.task_id:
                    raise RuntimeError(f"task {task.task_id} already handled by {t.name}")

            name = self._next_name()
            tasks.mark_in_progress(task.task_id, assignee=name)

            agent = self._build_agent(clone_tools(self.lead, _TEAMMATE_EXCLUDE))
            agent._mailbox = self.mailbox      # so a teammate's broadcast_notice works
            agent._agent_name = name

            # isolation: manager default unless the caller overrides
            want_wt = self.worktrees if worktree is None else worktree
            wt, branch, wt_note = None, None, None
            if want_wt:
                created = self._setup_worktree(name)
                if created is not None:
                    wt, branch = created
                else:
                    # never a silent degrade: the spawn result tells the Lead
                    # this teammate is NOT isolated (research tasks may be fine)
                    wt_note = (
                        "⚠ worktree isolation unavailable (not a git repo, or "
                        "git worktree add failed) — this teammate is running "
                        "WITHOUT isolation; if its task edits repo code, cancel "
                        "it or run the task sequentially."
                    )

            teammate = Teammate(name, agent, self.mailbox, tasks,
                                worktree=wt, branch=branch, on_event=self._notify)
            teammate._wt_note = wt_note
            teammate.current_task_id = task.task_id
            self._teammates[name] = teammate
            teammate.start()
            self.mailbox.post(name, {"from": "Lead", "kind": "task",
                                     "task_id": task.task_id,
                                     "content": task.description})
        self._notify("spawn", name=name, status="idle", task_id=task.task_id,
                     branch=branch, worktree=wt)
        return teammate

    # -- query / control ------------------------------------------------------

    def status(self) -> list[dict]:
        return [
            {"name": t.name, "status": t.status, "task_id": t.current_task_id}
            for t in self._teammates.values()
        ]

    def review(self, name: str, feedback: str) -> bool:
        t = self._teammates.get(name)
        if t is None or t.status == "ending":
            return False
        self.mailbox.post(name, {"from": "Lead", "kind": "review", "content": feedback})
        return True

    def release(self, name: str) -> bool:
        if name not in self._teammates:
            return False
        self.mailbox.post(name, {"from": "Lead", "kind": "end", "content": ""})
        # the teammate's branch is now the only place its work exists
        self._notify("release", name=name, status="ending",
                     branch=self._teammates[name]._branch,
                     worktree=self._teammates[name]._worktree)
        return True

    def release_all(self) -> None:
        for name in list(self._teammates):
            self.release(name)

    def collect(self) -> list[dict]:
        """Drain the Lead's inbox (results/notices), consuming them exactly once."""
        return self.mailbox.drain("Lead")

    def broadcast(self, content: str) -> list[str]:
        """Lead broadcast to all teammates (and their inboxes)."""
        return self.mailbox.broadcast(content, from_="Lead")

    # --------------------------------------------------------------------------- #
    # integrate - merge released worktree teammates back into the current branch
    # --------------------------------------------------------------------------- #

    def _ahead_of_head(self, branch: str) -> int:
        """How many commits ``branch`` has that the current HEAD does not."""
        cp = self._git(["rev-list", "--count", f"HEAD..{branch}"])
        try:
            return int((cp.stdout or "").strip() or 0)
        except ValueError:
            return 0

    def _discard_teammate(self, t: Teammate) -> None:
        """Remove a merged/cleaned teammate's worktree+branch and drop it from the map."""
        try:
            if t._worktree:
                self._git(["worktree", "remove", "--force", t._worktree])
                self._git(["branch", "-D", t._branch])
        finally:
            self._teammates.pop(t.name, None)

    def _integration_agent(self):
        from .agent import Agent              # local import: avoid circular import
        from .tools.agent import clone_tools  # local import: avoid circular import
        from .tools.bash import disable_confirmation  # local import: ditto
        agent = Agent(
            llm=self._integration_llm_for(),
            tools=clone_tools(self.lead, _TEAMMATE_EXCLUDE),
            max_context_tokens=self.lead.context.max_tokens,
            max_rounds=self.lead.max_rounds,
            memory_enabled=False,
        )
        disable_confirmation(agent.tools)     # headless: cannot confirm risky bash
        return agent

    def _resolve_conflict(self, t: Teammate, files: list[str]) -> str:
        """Run the one-shot Integration Agent to reconcile ``t``'s in-progress merge.

        The repo is mid-``git merge`` on ``t``'s branch. The agent must rewrite
        only the conflicted files, ``git add`` them, and ``git merge --continue``;
        then it runs the project tests and reports. Returns its report text.
        """
        task_result = ""
        if t.current_task_id:
            try:
                task = self.lead.tasks.get(t.current_task_id)
                task_result = getattr(task, "result", "") or ""
            except Exception:
                task_result = ""
        task_result = task_result[:_RESULT_TRIM] if task_result else "(none)"

        file_list = "\n".join(f"- {f}" for f in files) or "(none)"
        prompt = f"""\
The repository is mid-merge: branch {t._branch} (teammate {t.name}) is being \
merged into the current branch and the merge has genuine content conflicts. Other \
branches already merged cleanly are done -- do not touch them.

Conflicted files (resolve ONLY these, they currently contain <<<<<<< HEAD / \
======= / >>>>>>> {t._branch} markers):
{file_list}

You are the Integration Agent. Your job is to RECONCILE, not to pick one side.
1. Read each conflicted file. Understand both intents -- you may inspect either
   side: git show HEAD:<file>, git show {t._branch}:<file>, git log, git diff.
2. Rewrite each conflicted file into one coherent, correct merged version and
   remove all conflict markers. Respect both sides' real changes.
3. Touch ONLY the conflicted files above. Do NOT run `git add .`
4. git add each resolved file by its exact path.
5. Finish the merge: git merge --continue --no-edit
6. Run the project's tests (python -m pytest, or the project's command) and
   report what you reconciled per file, that the merge finished, and whether the
   tests pass.

If any conflict is genuinely ambiguous, do NOT force it: keep the markers, do not
continue the merge, and clearly explain what is ambiguous.

Teammate {t.name}'s own summary of this work (context on its intent):
{task_result}
"""
        try:
            agent = self._integration_agent()
            return agent.chat(prompt)
        except Exception as e:
            return f"[integrator error] {e}"

    def integrate(self) -> str:
        """Merge released (ending) worktree teammates back into the current branch.

        Deterministic git merges first; only a *genuine* merge conflict triggers
        the one-shot Integration Agent, which reconciles the conflicted files
        inside the merge and finishes it. Merged teammates' worktrees are removed.
        Returns a summary the Lead reviews before deciding final delivery.
        """
        candidates = [t for t in list(self._teammates.values())
                      if t.status == "ending" and t._worktree and t._branch]
        if not candidates:
            return ("(integrate_results) no released code teammates to merge. "
                    "Release a finished worktree teammate (release_teammate) "
                    "before integrating; idle teammates keep their worktrees.")

        lines: list[str] = []
        for t in candidates:
            t._commit_work()                       # best-effort final git commit
            if self._ahead_of_head(t._branch) == 0:
                self._discard_teammate(t)
                lines.append(f"- {t.name}: no repo changes to merge; worktree cleaned")
                continue

            cp = self._git(["merge", "--no-edit", t._branch])
            if cp.returncode == 0:
                stat = self._git(["show", "--stat", "--oneline", "HEAD"]).stdout.strip()
                self._discard_teammate(t)
                head = stat.splitlines()[0][:80] if stat else "merged"
                lines.append(f"- {t.name} ({t._branch}): merged cleanly → {head}")
                continue

            in_merge = (self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"])
                        .returncode == 0)
            if not in_merge:
                # refused, not a conflict: the current branch has local changes
                self._git(["merge", "--abort"])    # no-op, keeps the tree untouched
                lines.append(
                    f"- {t.name} ({t._branch}): NOT merged — the current branch "
                    "working tree has uncommitted changes that block the merge. "
                    "Commit/stash them, then call integrate_results again.")
                continue

            # genuine conflict -> Integration Agent reconciles inside the merge
            files = [f for f in self._git(
                ["diff", "--name-only", "--diff-filter=U"]).stdout.splitlines() if f]
            report = (self._resolve_conflict(t, files) or "").strip()
            still_merging = (self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"])
                             .returncode == 0)
            if still_merging:
                self._git(["merge", "--abort"])    # integrator did not finish
                lines.append(
                    f"- {t.name} ({t._branch}): conflict NOT resolved by the "
                    "Integration Agent; merge aborted (files/branch kept for manual "
                    f"handling).\n    integrator: {report[:_RESULT_TRIM]}")
            else:
                self._discard_teammate(t)
                lines.append(
                    f"- {t.name} ({t._branch}): conflicted → reconciled by the "
                    f"Integration Agent and merged.\n    {report[:_RESULT_TRIM]}")

        # the merge moved HEAD and removed worktrees: the environment changed in
        # a way no other event reports
        self._notify("integrate", status="merged",
                     head=self._git(["rev-parse", "--short", "HEAD"]).stdout.strip(),
                     merged=[t.name for t in candidates])
        header = "# integrate_results（已把 release 的代码 teammate 归并进当前分支）"
        return "\n".join([header] + lines)

    def render_summary(self, results: list[dict], statuses: list[dict]) -> str:
        """Deterministic '队友结果摘要' block (not LLM-generated)."""
        if not results and not statuses:
            return ""
        lines = ["# 队友结果摘要（Lead 审阅用，仅供参考）"]
        for r in results:
            src = r.get("from", "?")
            content = (r.get("content") or "").strip()
            if len(content) > _RESULT_TRIM:
                content = content[:_RESULT_TRIM] + "…"
            if r.get("task_id"):
                lines.append(f"- [{src}] task {r['task_id']} → {content}")
            else:
                lines.append(f"- [{src}] {content}")
        if statuses:
            st = " · ".join(
                f"{s['name']} {s['status']}"
                + (f"({s['task_id']})" if s["task_id"] else "")
                for s in statuses)
            lines.append(f"队友状态：{st}")
        return "\n".join(lines)
