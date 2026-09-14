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
from .tools.checkpoint import CheckpointTool
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
        checkpoint_enabled: bool = False,
        checkpoint_dir: str | None = None,
        checkpoint_keep: int = 10,
        checkpoint_max: int = 50,
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
                         ReleaseTeammateTool, BroadcastNoticeTool, IntegrateResultsTool,
                         CheckpointTool)
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

        # checkpoint / breakpoint recovery (design_ckeckpoint.md): OFF by default,
        # and deliberately opt-in per Agent -- TeamManager builds a fresh Agent
        # per teammate (team.py:_build_agent), and those must not each open their
        # own .CHECKPOINT/ dir. Only the Lead the user is talking to checkpoints.
        self.checkpoints = None
        if checkpoint_enabled:
            from .checkpoint import BASE_DIRNAME, CheckpointManager
            self.checkpoints = CheckpointManager(
                agent=self,
                base_dir=checkpoint_dir or BASE_DIRNAME,
                keep=checkpoint_keep,
                max_checkpoints=checkpoint_max,
                llm=self.llm,
            )
            # observation hooks: the state itself is read from disk when a
            # snapshot is taken, so these events are the trail, not the truth
            self.todos.on_change = self._on_todo_change
            self.tasks.on_change = self._on_task_change
            if self.team is not None:
                self.team.on_event = self._on_team_event
            self.checkpoints.record("session_start", actor="system",
                                    name=self.checkpoints.session_id)

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

        # a bare "carry on" after a restore becomes "继续：<the task that was in
        # flight>". Done here, before the prelude is assembled, because the task
        # text is the request and the prelude is context riding alongside it --
        # and because the rewrite is idempotent (wrap_continue strips first), so
        # restoring the same checkpoint twice cannot stack prefixes.
        raw_input = user_input
        if self.checkpoints is not None:
            user_input = self.checkpoints.resume_for_input(user_input)

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
        # a restored checkpoint's handoff note rides in the same channel: the
        # recovered agent must re-orient before working, so it is assembled from
        # the snapshot at restore time and consumed here, exactly once.
        if self.checkpoints is not None:
            handoff = self.checkpoints.take_handoff()
            if handoff:
                prelude.insert(0, handoff)
        if prelude:
            user_input = "\n\n".join(prelude + [user_input])

        self.messages.append({"role": "user", "content": user_input})
        # ``input`` is what the *user* typed, ``data.prompt`` what was actually
        # sent. v1 logged only the augmented text, so the raw request was gone
        # -- and the augmentations quote older handoffs, i.e. the resume text
        # was already accumulating the very prefixes §11.1 exists to prevent.
        self._record("user_message", actor="user", input=raw_input,
                     data={"raw": raw_input, "prompt": user_input})
        self.maybe_compress()

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
                # a completed turn is a natural boundary: always worth a snapshot
                self._record("turn_end", actor="lead", output=reply)
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
                    self._record_tool(tc, result)
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                        self._record_tool(tc, result)
            except KeyboardInterrupt:
                # Snapshot BEFORE the backfill: the snapshot then holds exactly
                # what a crash leaves behind -- an assistant message whose
                # tool_calls have no replies -- and restore repairs it with
                # repair_chain. Both are the same shape on purpose.
                self._record("interrupt", actor="user", name=resp.tool_calls[0].name,
                             data={"pending": [tc.id for tc in resp.tool_calls]})
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise

            # compress if tool outputs are big
            self.maybe_compress()

        return "(reached maximum tool-call rounds)"

    # -- checkpoint wiring ----------------------------------------------------- #

    def _record(self, event_type: str, **fields):
        """Append an event; the checkpoint layer decides whether to snapshot."""
        if self.checkpoints is None:
            return None
        return self.checkpoints.record(event_type, **fields)

    def maybe_compress(self) -> bool:
        """Compress context, snapshotting before anything irreversible happens.

        Layers 2 and 3 rewrite ``messages`` in place (``messages.clear()``) --
        the only irreversible context operation in the project, and exactly the
        "截断时的 work_state" the design calls out. Layer 1 only trims verbose
        tool output, so it is recorded but not snapshotted: snapshotting on it
        would fire on most rounds of a long session ("不要无脑保存").

        Call *this* rather than ``context.maybe_compress`` directly: the two
        hooks below are the entire safety net, and going straight to the context
        layer silently drops both the pre-truncation snapshot and the write-back
        of durable facts into memory. ``/compact`` is the caller that matters --
        it is the one place a user asks for truncation on purpose.
        """
        def before(layer: str):
            if self.checkpoints is None:
                return
            if layer == "snip":
                self.checkpoints.record("compress", actor="system", name=layer,
                                        status="snip")
            else:
                self.checkpoints.snapshot(trigger="compress", actor="system",
                                          force=True, label=f"上下文{layer}前")

        def after(summary: str):
            # remember where the transcript was cut, so a handoff can point back
            # into the event log for the actions the summary swallowed
            if self.checkpoints is not None:
                self.checkpoints.mark_compressed(summary)
            if self.memory is not None and self.memory_enabled:
                self._on_compress(summary)

        return self.context.maybe_compress(self.messages, self.llm,
                                           on_compress=after,
                                           before_compress=before)

    def _record_tool(self, tc, result: str) -> None:
        """Log a tool result, and an approval event when a human is being asked.

        ``trigger_for`` drops the read-only tools, so this is cheap on the
        common path: the event always lands in the log, only write/edit/bash/
        team tools can raise it to a snapshot.

        ``data.args`` carries the arguments themselves, not just ``str(args)``:
        the snapshot has to be able to say *what* was running when it stopped,
        and a failed call's arguments are half of "why it stopped" (v2 §11.2a).
        """
        if self.checkpoints is None:
            return
        from .tools.bash import take_pending_approval
        text = result or ""
        pending = take_pending_approval()
        # "waiting for a human" is not a failure: the ⛔ prefix would otherwise
        # classify it as one, and last_error would report the agent as stuck on
        # a call that is merely unanswered (a "⛔ Refused" in a teammate has no
        # pending approval and stays an error -- it really did fail).
        if pending:
            status = "pending"
        else:
            status = ("error" if text.startswith(("Error", "⚠", "⛔")) else "ok")
        self.checkpoints.record(
            "tool_done", actor="lead", name=tc.name, tool=tc.name,
            input=str(tc.arguments), output=text, status=status,
            files=self._tool_files(tc),
            data={"args": dict(tc.arguments or {})},
        )
        if pending:
            # the design's ④/⑤: paused for a human decision is a hard boundary,
            # and the pending command must survive the process dying. One event,
            # not two: this *is* the tool_done of that bash call, and emitting a
            # second snapshot one line later only duplicated the state.
            self.checkpoints.record(
                "approval", actor="system", name=pending.get("tool", tc.name),
                tool=pending.get("tool", tc.name), input=str(tc.arguments),
                output=text, status="pending",
                data={"command": pending.get("command", ""),
                      "reason": pending.get("reason", ""),
                      "args": dict(tc.arguments or {})},
            )

    @staticmethod
    def _tool_files(tc) -> list[str]:
        """The file a write/edit touched -- a pointer, never the content."""
        args = tc.arguments or {}
        path = args.get("file_path") or args.get("path")
        return [str(path)] if path else []

    def _on_todo_change(self, action: str, todo=None) -> None:
        self._record("todo_changed", actor="lead", name=getattr(todo, "todo_id", ""),
                     status=getattr(todo, "status", ""),
                     data={"action": action, "title": getattr(todo, "title", "")})

    def _on_task_change(self, task, previous: str | None) -> None:
        self._record("task_changed", actor="lead", name=task.task_id,
                     status=task.state,
                     data={"from": previous or "", "to": task.state,
                           "description": task.description})

    def _on_team_event(self, action: str, name: str = "", **fields) -> None:
        self._record("teammate_changed", actor="lead", name=name,
                     status=fields.get("status", ""),
                     data={"action": action, **fields})

    def _on_compress(self, summary: str):
        """Callback from ContextManager: pull durable facts out of a compressed
        summary into long-term memory, so nothing is lost on context collapse."""
        try:
            self.memory.ingest_summary(summary)
        except Exception:
            pass

    # -- restore --------------------------------------------------------------- #

    def restore_state(self, cp_id: str) -> tuple[bool, str]:
        """Pull the agent back to a checkpoint. Returns ``(ok, message)``.

        Restores the agent's own state only -- messages (with the chain repaired)
        and the todo list. Files are never touched; ``.TASK/`` is read fresh from
        disk, so it needs no restoring. The handoff note is parked and injected
        into the next request as a prelude, because a restored agent must
        re-orient before it works.

        The state swap goes in through ``apply`` rather than happening after the
        call: the manager takes the rewind snapshot itself, and it has to be
        taken once the agent has actually moved, or the new head describes the
        world the user just left and re-restoring it undoes the restore.
        """
        if self.checkpoints is None:
            return False, "checkpoint 未启用（用 ENCODER_CHECKPOINT_ENABLED=1 开启）"
        if not self.checkpoints.adopt(cp_id):
            return False, f"未找到断点 {cp_id}"

        def apply_state(restored):
            self.messages.clear()
            self.messages.extend(restored.messages)
            self.todos.restore(restored.todos)

        restored = self.checkpoints.restore(cp_id, apply=apply_state)
        if restored is None:
            return False, f"断点 {cp_id} 读取失败（文件可能已损坏）"
        return True, restored.handoff

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
        # ⑤: a marker written *before* a call that is likely to run long. Its whole
        # value is that it exists when the call does not come back -- a command
        # killed halfway (SIGKILL, a closed terminal, Ctrl+C inside the tool) is
        # answered by this event and nothing else, because `tool_done` never runs.
        # Emitted here rather than at the call sites because both the single and
        # the parallel path funnel through this one, and because it is *literally*
        # after the argument check: a call that never starts deserves no marker.
        if self.checkpoints is not None:
            from .checkpoint import looks_long
            args = dict(tc.arguments or {})
            if looks_long(tc.name, args):
                self._record("tool_start", actor="lead", name=tc.name, tool=tc.name,
                             input=str(tc.arguments),
                             data={"long": True, "args": args})
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
        # snapshot first: /reset is the one moment the messages are about to be
        # gone for good, so this must be recoverable
        self._record("rewind", actor="user", data={"action": "reset"},
                     name=str(len(self.messages)))
        self.messages.clear()
        self.todos.clear()
