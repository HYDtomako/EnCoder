"""Task tools - persisted work units with dependencies, dispatched to an agent.

The Lead creates tasks (``create_task``), dispatches them (``dispatch_task``),
lists / tweaks them (``list_tasks`` / ``update_task``) and archives a finished
root task (``archive_tasks``). State changes always go through ``TaskManager`` -
tools never touch ``.TASK/`` files directly.
"""

from ..task import MAX_ATTEMPTS, PRIORITIES, STATES
from .agent import spawn_subagent
from .base import Tool


class CreateTaskTool(Tool):
    name = "create_task"
    description = (
        "Persist a work unit (task). Tasks are saved to .TASK/, may depend on "
        "other tasks via blocked_by, and are later run with dispatch_task. A task "
        "can only start once every blocked_by task is completed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Overall description of the task",
            },
            "agent": {
                "type": "string",
                "description": "Optional: the agent that should handle it "
                               "(assigned at dispatch if left empty)",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: task ids this task depends on",
            },
            "priority": {
                "type": "string",
                "description": "high | normal | low (default normal)",
            },
        },
        "required": ["description"],
    }

    # set by Agent.__init__ (like AgentTool._parent_agent)
    _parent_agent = None

    def execute(self, description: str, agent: str = "",
                blocked_by: list[str] | None = None,
                priority: str = "normal") -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: task tools not initialized"
        try:
            task = parent.tasks.create(
                description, agent=agent, blocked_by=blocked_by, priority=priority)
        except ValueError as e:
            return f"Error: {e}"
        line = f"Created task [{task.task_id}] \"{task.description}\" (priority: {task.priority})"
        if task.blockedBy:
            line += f" blockedBy: {task.blockedBy}"
        return line


class ListTasksTool(Tool):
    name = "list_tasks"
    description = (
        "List all active tasks with state, priority, and dependencies. Use this "
        "to see what is ready to dispatch and what is still blocked."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    _parent_agent = None

    def execute(self) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: task tools not initialized"
        tasks = parent.tasks.list()
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            line = f"  [{t.task_id}] {t.state:<11} {t.description}"
            if t.priority != "normal":
                line += f"  priority: {t.priority}"
            if t.blockedBy:
                line += f"  blockedBy: {t.blockedBy}"
            if t.last_error:
                line += f"  last_error: {t.last_error}"
            lines.append(line)
        return f"Tasks ({len(tasks)}):\n" + "\n".join(lines)


class UpdateTaskTool(Tool):
    name = "update_task"
    description = (
        "Manually adjust a task's state / priority / note. Still validated by "
        "TaskManager - use for Lead fallback (e.g. a task done inline)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task id"},
            "state": {"type": "string", "description": "pending | in_progress | completed"},
            "priority": {"type": "string", "description": "high | normal | low"},
            "note": {"type": "string", "description": "Optional short note"},
        },
        "required": ["task_id"],
    }

    _parent_agent = None

    def execute(self, task_id: str, state: str | None = None,
                priority: str | None = None, note: str | None = None) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: task tools not initialized"
        if state is not None and state not in STATES:
            return f"Error: invalid state '{state}', expected one of {list(STATES)}"
        if priority is not None and priority not in PRIORITIES:
            return f"Error: invalid priority '{priority}', expected one of {list(PRIORITIES)}"
        try:
            task = parent.tasks.update(task_id, state=state, priority=priority, note=note)
        except (ValueError, KeyError) as e:
            return f"Error: {e}"
        return (f"[{task.task_id}] state={task.state} priority={task.priority}"
                + (f" note={task.note}" if task.note else ""))


class DispatchTaskTool(Tool):
    name = "dispatch_task"
    description = (
        "Dispatch a task to an agent to execute. Checks dependencies first and "
        "refuses to start a blocked or already-running task, enforces a max of "
        f"{MAX_ATTEMPTS} attempts, then marks the task in_progress, runs it, and "
        "writes the result back as completed. Dispatches ONE task at a time - "
        "call it repeatedly as dependencies complete, listing ready tasks with "
        "list_tasks in between."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task id to dispatch",
            },
        },
        "required": ["task_id"],
    }

    _parent_agent = None

    def execute(self, task_id: str) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: task tools not initialized"
        tm = parent.tasks
        task = tm.get(task_id)
        if task is None:
            return f"Error: no such task '{task_id}'"
        if task.state == "completed":
            return f"Task {task_id} already completed."
        if task.state == "in_progress" and task.last_error is None:
            return f"Error: task {task_id} is already in progress."
        if task.last_error and task.attempts >= MAX_ATTEMPTS:
            return (f"Error: task {task_id} reached max dispatch attempts "
                    f"({MAX_ATTEMPTS}). Recreate or fix the task, then dispatch again.")

        # mark_in_progress does the dependency check AND allows a failed task to
        # be retried (in_progress + last_error). Checking can_start first here
        # would wrongly reject retries, because a failed task is not 'pending'.
        try:
            tm.mark_in_progress(task_id, assignee="main")
        except (KeyError, RuntimeError) as e:
            return f"Error: {e}"

        try:
            result = spawn_subagent(parent, task.description)
        except Exception as e:
            t = tm.get(task_id)
            t.last_error = str(e)
            tm.save(t)
            return f"Error dispatching {task_id}: {e}"

        try:
            tm.mark_completed(task_id, result)
        except RuntimeError as e:
            return f"Error: {e}"
        return f"Task {task_id} completed.\n{result}"


class ArchiveTasksTool(Tool):
    name = "archive_tasks"
    description = (
        "Archive the whole dependency subgraph of a *completed* root (overall) "
        "task into a single aggregate file under .TASK/done/. Refuses while the "
        "root or any sub-task is unfinished."
    )
    parameters = {
        "type": "object",
        "properties": {
            "root_id": {
                "type": "string",
                "description": "Task id of the root / overall task",
            },
        },
        "required": ["root_id"],
    }

    _parent_agent = None

    def execute(self, root_id: str) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: task tools not initialized"
        try:
            archived = parent.tasks.archive(root_id)
        except (KeyError, RuntimeError) as e:
            return f"Error: {e}"
        return (f"Archived {len(archived)} task(s) into .TASK/done/:\n"
                + "\n".join(f"  {t.task_id}  {t.description}" for t in archived))
