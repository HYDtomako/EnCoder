"""Todo tools - session-scoped checklist the Lead maintains for the current request.

Todos are a lightweight plan: created via a tool (default status pending),
updated as the work progresses, listed on demand. They are never persisted or
dispatched - that is the Task tools' job. All tools strictly inherit
``tools/base.py``'s ``Tool``.
"""

from ..task import STATES
from .base import Tool


class CreateTodoTool(Tool):
    name = "create_todo"
    description = (
        "Plan 1..N todo checklist items for the current request, all starting as "
        "pending. Todos are a session-scoped plan you maintain; they are not "
        "persisted and not dispatched. Use for simple steps you will do yourself."
    )
    parameters = {
        "type": "object",
        "properties": {
            "titles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One-line titles of the todos to create",
            },
            "task_id": {
                "type": "string",
                "description": "Optional: the task this todo belongs to",
            },
        },
        "required": ["titles"],
    }

    # set by Agent.__init__ (like AgentTool._parent_agent)
    _parent_agent = None

    def execute(self, titles: list[str], task_id: str | None = None) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: todo tools not initialized"
        titles = [str(t).strip() for t in (titles or []) if str(t).strip()]
        if not titles:
            return "Error: titles must not be empty"
        created = parent.todos.create(titles, task_id=task_id)
        if not created:
            return "Error: titles must not be empty"
        return "Created todos:\n" + "\n".join(
            f"  [{t.todo_id}] {t.status}  {t.title}" for t in created)


class UpdateTodoTool(Tool):
    name = "update_todo"
    description = (
        "Update a todo item's status (pending/in_progress/completed) or note."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The todo id, e.g. t1",
            },
            "status": {
                "type": "string",
                "description": "pending | in_progress | completed",
            },
            "note": {
                "type": "string",
                "description": "Optional short note",
            },
        },
        "required": ["todo_id"],
    }

    _parent_agent = None

    def execute(self, todo_id: str, status: str | None = None,
                note: str | None = None) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: todo tools not initialized"
        if status is not None and status not in STATES:
            return f"Error: invalid status '{status}', expected one of {list(STATES)}"
        try:
            todo = parent.todos.update(todo_id, status=status, note=note)
        except ValueError as e:
            return f"Error: {e}"
        if todo is None:
            return f"Error: no todo '{todo_id}'"
        line = f"[{todo.todo_id}] {todo.status}  {todo.title}"
        if todo.note:
            line += f"  ({todo.note})"
        return line


class ListTodosTool(Tool):
    name = "list_todos"
    description = "List the current request's todo checklist."
    parameters = {"type": "object", "properties": {}, "required": []}

    _parent_agent = None

    def execute(self) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: todo tools not initialized"
        todos = parent.todos.list()
        if not todos:
            return "No todos."
        return "Todos:\n" + "\n".join(
            f"  [{t.todo_id}] {t.status}  {t.title}" for t in todos)
