"""Team tools (add3.0): establish and drive a team of persistent teammates.

The Lead creates tasks (``create_task``) and hands them to teammates
(``spawn_teammate``). A teammate runs in parallel on its own thread and reports
through the mailbox; ``collect_results`` pulls those results back, ``review_teammate``
sends a correction (prioritised over a queued task), ``release_teammate`` ends one,
and ``broadcast_notice`` shares a global finding with the other agents.

Teammate mode is OFF by default and only the user turns it on (``/team on``);
the Lead merely suggests. Until then every tool here returns the same hint, so
the Lead cannot spin up a team on its own.
"""

from .base import Tool

_OFF_HINT = "teammate mode is off; enable with /team on (Lead only suggests, user decides)"


def _team_of(parent):
    """Return the parent's TeamManager, or None when teammate mode is off."""
    return getattr(parent, "team", None)


class SpawnTeammateTool(Tool):
    name = "spawn_teammate"
    description = (
        "Hand a task to a persistent teammate that runs it in parallel on its own "
        "thread and reports the result to your mailbox. Unlike a sub-agent, the "
        "teammate stays alive (idle) for more tasks or your review. Requires teammate "
        "mode to be on. One task = one teammate; at most a handful run at once."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task id to assign (from .TASK/); required unless description given",
            },
            "description": {
                "type": "string",
                "description": "Or: a task description; a task is auto-created then assigned",
            },
            "worktree": {
                "type": "boolean",
                "description": "Isolate this teammate in a .git/worktree (optional)",
            },
        },
        "required": [],
    }

    _parent_agent = None

    def execute(self, task_id: str | None = None, description: str | None = None,
                worktree: bool = False) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: team tools not initialized"
        if _team_of(parent) is None:
            return _OFF_HINT
        try:
            teammate = parent.team.spawn(task_id=task_id, description=description,
                                         worktree=worktree)
        except (KeyError, ValueError, RuntimeError) as e:
            return f"Error: {e}"
        tid = teammate.current_task_id
        task = parent.tasks.get(tid)
        label = f' "{task.description}"' if task is not None else ""
        return (f"Spawned teammate {teammate.name} for task [{tid}]{label} "
                f"(status: {teammate.status}). It runs in parallel and will report "
                f"its result to your mailbox.")


class CollectResultsTool(Tool):
    name = "collect_results"
    description = (
        "Drain your mailbox and return a summary of teammate results and statuses. "
        "Call this to wait for / collect what the teammates have finished."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    _parent_agent = None

    def execute(self) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: team tools not initialized"
        if _team_of(parent) is None:
            return _OFF_HINT
        summary = parent.team.render_summary(parent.team.collect(), parent.team.status())
        return summary or "No teammates and no new results."


class ReviewTeammateTool(Tool):
    name = "review_teammate"
    description = (
        "Send review feedback to an idle teammate. The feedback continues the "
        "teammate's own context, so it can revise its last task. Prioritised over "
        "any queued new task for that teammate."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Teammate name, e.g. agent_1"},
            "feedback": {"type": "string", "description": "Correction / follow-up to give"},
        },
        "required": ["name", "feedback"],
    }

    _parent_agent = None

    def execute(self, name: str, feedback: str) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: team tools not initialized"
        if _team_of(parent) is None:
            return _OFF_HINT
        if parent.team.review(name, feedback):
            return f"Review sent to {name} (prioritised over any queued task)."
        return f"Error: no active teammate named '{name}'."


class ReleaseTeammateTool(Tool):
    name = "release_teammate"
    description = (
        "Tell a teammate to end: it finishes its current work, then shuts down "
        "(status ending). Until released, a teammate stays ready for more tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Teammate name, e.g. agent_1"},
        },
        "required": ["name"],
    }

    _parent_agent = None

    def execute(self, name: str) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: team tools not initialized"
        if _team_of(parent) is None:
            return _OFF_HINT
        if parent.team.release(name):
            return f"Sent end to {name}; it will shut down after its current work."
        return f"Error: no teammate named '{name}'."


class BroadcastNoticeTool(Tool):
    name = "broadcast_notice"
    description = (
        "Broadcast a global finding (e.g. a broken dependency or a wrong config) "
        "to the other agents. Each agent receives it in its mailbox and will see it "
        "before its next task. Available to teammates as well as the Lead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The finding to share"},
        },
        "required": ["content"],
    }

    _parent_agent = None

    def execute(self, content: str) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: team tools not initialized"
        team = getattr(parent, "team", None)
        if team is not None:
            targets = team.mailbox.broadcast(content, from_="Lead")
        else:
            # called from inside a teammate (its Agent has no TeamManager, but
            # carries a _mailbox/_agent_name); the Lead with team off has neither.
            mailbox = getattr(parent, "_mailbox", None)
            if mailbox is None:
                return _OFF_HINT
            name = getattr(parent, "_agent_name", None)
            targets = mailbox.broadcast(content, from_=name or "?")
        return f"Broadcast to {len(targets)} agent(s): {targets}"
