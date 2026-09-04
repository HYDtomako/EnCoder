"""Core agent loop.

This is the heart of Encoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
from .llm import LLM
from .tools import ALL_TOOLS
from .tools.base import Tool
from .tools.agent import AgentTool
from .tools.todo import CreateTodoTool, UpdateTodoTool, ListTodosTool
from .tools.task import (
    CreateTaskTool, ListTasksTool, UpdateTaskTool, DispatchTaskTool, ArchiveTasksTool,
)
from .tools.team import (
    SpawnTeammateTool, CollectResultsTool, ReviewTeammateTool,
    ReleaseTeammateTool, BroadcastNoticeTool, IntegrateResultsTool,
)
from .prompt import system_prompt
from .context import ContextManager
from .task import TodoList, TaskManager
from .team import TeamManager


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        scheduler=None,
        memory_enabled: bool = True,
        memory_llm: str | None = None,
        team_enabled: bool = False,
        team_worktrees: bool = True,
        team_max: int = 3,
        team_model: str | None = None,
        team_api_key: str | None = None,
        team_base_url: str | None = None,
        integration_model: str | None = None,
        integration_api_key: str | None = None,
        integration_base_url: str | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self._tool_by_name = {t.name: t for t in self.tools}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools)

        # scheduler for daily recurring tasks; defaults to the process-wide one
        if scheduler is None:
            from .cron_scheduler import get_scheduler
            scheduler = get_scheduler()
        self.scheduler = scheduler

        # session-scoped todo list + persistent task manager (DESIGN_todo_task_v1)
        self.todos = TodoList()
        self.tasks = TaskManager()

        # wire up tools that need a reference back to this agent
        _PARENT_TOOLS = (AgentTool, CreateTodoTool, UpdateTodoTool, ListTodosTool,
                         CreateTaskTool, ListTasksTool, UpdateTaskTool,
                         DispatchTaskTool, ArchiveTasksTool,
                         SpawnTeammateTool, CollectResultsTool, ReviewTeammateTool,
                         ReleaseTeammateTool, BroadcastNoticeTool, IntegrateResultsTool)
        for t in self.tools:
            if isinstance(t, _PARENT_TOOLS):
                t._parent_agent = self

        # persistent memory: recalled into the system prompt as soft context,
        # extracted from each finished round. Never allowed to break the loop.
        self.memory_enabled = memory_enabled
        self.memory = None
        if memory_enabled:
            from .memory import MemoryManager
            self.memory = MemoryManager(llm=self.llm, memory_model=memory_llm)

        # multi-agent team (add3.0): OFF by default - only the user enables it
        self.team_enabled = team_enabled
        self.team_worktrees = team_worktrees   # default isolation for code teammates
        self.team_model = team_model
        self.team_api_key = team_api_key
        self.team_base_url = team_base_url
        self.integration_model = integration_model
        self.integration_api_key = integration_api_key
        self.integration_base_url = integration_base_url
        self.team = (TeamManager(lead=self, worktrees=team_worktrees,
                                 max_teammates=team_max,
                                 team_model=team_model,
                                 team_api_key=team_api_key,
                                 team_base_url=team_base_url,
                                 integration_model=integration_model,
                                 integration_api_key=integration_api_key,
                                 integration_base_url=integration_base_url)
                     if team_enabled else None)

    def _full_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        # recall relevant memories into the system prompt (soft context only:
        # a suggestion that the current user request always outranks)
        memory_block = None
        if self.memory is not None and self.memory_enabled:
            memory_block = self.memory.recall(user_input, self.messages[-6:])
        self._system = system_prompt(self.tools, memory_block=memory_block)

        # deterministic prelude: persisted unfinished tasks first (xuigai2.0:
        # the Lead must see leftover .TASK/ work every request and judge for
        # itself whether to continue/archive it), then teammate results. The
        # real user request stays the last user message (recent = strongest).
        prelude: list[str] = []
        reminder = self.tasks.pending_reminder()
        if reminder:
            prelude.append(reminder)
        if self.team is not None:
            summary = self.team.render_summary(self.team.collect(), self.team.status())
            if summary:
                prelude.append(summary)
        if prelude:
            user_input = "\n\n".join(prelude + [user_input])

        self.messages.append({"role": "user", "content": user_input})
        self.context.maybe_compress(
            self.messages, self.llm,
            on_compress=self._on_compress if self.memory is not None and self.memory_enabled else None,
        )

        for _ in range(self.max_rounds):
            resp = self.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=on_token,
            )

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                reply = resp.content
                # persist anything durable from this finished round
                if self.memory is not None and self.memory_enabled:
                    try:
                        result = self.memory.extract_and_store(self.messages[-4:])
                        if getattr(result, "is_notice", False):
                            reply = self._append_memory_notice(reply, result)
                    except Exception:
                        pass  # memory must never break the agent loop
                return reply

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)

            try:
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    if on_tool:
                        on_tool(tc.name, tc.arguments)
                    result = self._exec_tool(tc)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
            except KeyboardInterrupt:
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise

            # compress if tool outputs are big
            self.context.maybe_compress(
                self.messages, self.llm,
                on_compress=self._on_compress if self.memory is not None and self.memory_enabled else None,
            )

        return "(reached maximum tool-call rounds)"

    def _on_compress(self, summary: str):
        """Callback from ContextManager: pull durable facts out of a compressed
        summary into long-term memory, so nothing is lost on context collapse."""
        try:
            self.memory.ingest_summary(summary)
        except Exception:
            pass

    def _append_memory_notice(self, reply: str, result) -> str:
        """Append a deterministic conflict notice (not LLM-generated) so the
        user can decide whether to update a contradicted memory."""
        notice = (
            f"\n\n（记忆）之前记录：{result.topic} · {result.old}（{result.old_date}）\n"
            f"你刚才说：{result.new}。需要更新记忆吗？—— /memory resolve 查看与处理"
        )
        return (reply or "") + notice

    def _exec_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
            return [f.result() for f in futures]

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[interrupted]",
                })

    def reset(self):
        """Clear conversation history and the session-scoped todo list.
        Persisted tasks in .TASK/ are intentionally kept."""
        self.messages.clear()
        self.todos.clear()
