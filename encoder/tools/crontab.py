"""Scheduled-task tools - let the agent register daily recurring tasks.

The tools talk to the shared CronScheduler singleton so tasks persist
across sessions under ~/.encoder/tasks.json.
"""

from ..cron_scheduler import get_scheduler, valid_time
from .base import Tool


def _scheduler():
    return get_scheduler()


def _format_task(t) -> str:
    last = t.last_fired or "never"
    return f"- [{t.task_id}] {t.time} \"{t.content}\" (last fired: {last})"


class CreateScheduleTool(Tool):
    name = "create_schedule"
    description = (
        "Register a daily recurring task: at the given HH:MM time each day the "
        "agent will run the task content automatically, even when the user isn't "
        "saying anything. The task stays until deleted. Use this instead of doing "
        "a recurring request only once."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "What the agent should do, e.g. 'collect today's AI news and summarize it for me'",
            },
            "time": {
                "type": "string",
                "description": "Daily trigger time in HH:MM 24-hour format, e.g. '08:00'",
            },
        },
        "required": ["content", "time"],
    }

    def execute(self, content: str, time: str) -> str:
        if not content.strip():
            return "Error: content must not be empty"
        if not valid_time(time):
            return f"Error: invalid time '{time}', expected HH:MM (24-hour, e.g. '08:00')"
        task_id = _scheduler().register_task(content.strip(), time)
        return f"Scheduled: every day at {time} the agent will run:\n  {content}\n(task id: {task_id})"


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = "List all scheduled tasks with their id, time, content, and last-fired date."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self) -> str:
        tasks = _scheduler().list_tasks()
        if not tasks:
            return "No scheduled tasks."
        return "Scheduled tasks:\n" + "\n".join(_format_task(t) for t in tasks)


class DeleteScheduleTool(Tool):
    name = "delete_schedule"
    description = "Delete a scheduled task by its task id (see list_schedules)."
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task id to delete",
            },
        },
        "required": ["task_id"],
    }

    def execute(self, task_id: str) -> str:
        if _scheduler().delete_task(task_id):
            return f"Deleted task {task_id}."
        return f"Error: no task with id '{task_id}'"