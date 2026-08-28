"""Sub-agent spawning (inspired by Claude Code's AgentTool, 1397 lines).

The idea: for complex sub-tasks, spawn an independent agent with its own
conversation history and tool access. This lets the main agent delegate
work like "go research this codebase and report back" without polluting
its own context window.

The sub-agent runs to completion and returns a text summary.
"""

from .base import Tool

# tools a sub-agent must not carry: spawning/dispatching further agents
_SUBAGENT_EXCLUDE = frozenset({"agent", "dispatch_task"})


def clone_tools(lead, exclude: frozenset) -> list[Tool]:
    """Fresh instances of the same tool *types* (share the registry, not instances).

    Each agent must own its tool objects because ``Agent.__init__`` wires
    ``_parent_agent`` onto them; sharing instances would let the last-built agent
    overwrite every other agent's parent reference. Shared by ``spawn_subagent``
    and by ``TeamManager.spawn`` for teammates.
    """
    return [type(t)() for t in lead.tools if t.name not in exclude]


def spawn_subagent(parent, task: str) -> str:
    """Create a fresh sub-agent, run ``task``, return the (trimmed) result.

    Shared by the ``agent`` tool and ``dispatch_task``. The sub-agent gets its
    own context and tool access, but no ``agent`` / ``dispatch_task`` tools, so
    it can't spawn or dispatch recursively.
    """
    # import here to avoid a circular import at module load time
    from ..agent import Agent

    sub = Agent(
        llm=parent.llm,
        tools=clone_tools(parent, _SUBAGENT_EXCLUDE),
        max_context_tokens=parent.context.max_tokens,
        max_rounds=20,
    )
    result = sub.chat(task)
    # trim long results to avoid blowing up parent's context
    if len(result) > 5000:
        result = result[:4500] + "\n... (sub-agent output truncated)"
    return result


class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn a sub-agent to handle a complex sub-task independently. "
        "The sub-agent has its own context and tool access. Use this for: "
        "researching a codebase, implementing a multi-step change in isolation, "
        "or any task that would benefit from a fresh context window."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What the sub-agent should accomplish",
            },
        },
        "required": ["task"],
    }

    # set by Agent.__init__ after construction
    _parent_agent = None

    def execute(self, task: str) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"
        parent = self._parent_agent
        try:
            result = spawn_subagent(parent, task)
            return f"[Sub-agent completed]\n{result}"
        except Exception as e:
            return f"Sub-agent error: {e}"
