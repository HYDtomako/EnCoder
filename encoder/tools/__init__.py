"""Tool registry."""

from .bash import BashTool
from .read import ReadFileTool
from .write import WriteFileTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .agent import AgentTool
from .crontab import CreateScheduleTool, ListSchedulesTool, DeleteScheduleTool
from .web_search import WebSearchTool
from .web_fetch import WebFetchTool
from .todo import CreateTodoTool, UpdateTodoTool, ListTodosTool
from .task import CreateTaskTool, ListTasksTool, UpdateTaskTool, DispatchTaskTool, ArchiveTasksTool
from .team import SpawnTeammateTool, CollectResultsTool, ReviewTeammateTool, ReleaseTeammateTool, BroadcastNoticeTool, IntegrateResultsTool
from .checkpoint import CheckpointTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    AgentTool(),
    CreateScheduleTool(),
    ListSchedulesTool(),
    DeleteScheduleTool(),
    WebSearchTool(),
    WebFetchTool(),
    CreateTodoTool(),
    UpdateTodoTool(),
    ListTodosTool(),
    CreateTaskTool(),
    ListTasksTool(),
    UpdateTaskTool(),
    DispatchTaskTool(),
    ArchiveTasksTool(),
    SpawnTeammateTool(),
    CollectResultsTool(),
    ReviewTeammateTool(),
    ReleaseTeammateTool(),
    BroadcastNoticeTool(),
    IntegrateResultsTool(),
    CheckpointTool(),
]


def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None
