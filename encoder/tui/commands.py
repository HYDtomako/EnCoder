"""TUI 命令处理器 —— 复刻 ``cli.py`` 里所有 ``/命令``,输出为带样式的 ``rich.text.Text`` 行。

原则:
- **复用**:直接读 ``agent.tasks / todos / memory / team / scheduler / llm`` 等属性,
  与 cli.py 的 `/memory`、`/task`、`/team` 使用同一批数据源,不改任何核心模块。
- **不重构 cli**:这里是一份为 TUI 服务的轻量实现,``cli.py`` 的打印函数原样保留。
- 需要用户二次确认的地方返回 :class:`ConfirmRequest`,由 App 弹模态后回调
  :meth:`CommandRunner.handle_confirm`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text

from .. import __version__
from ..agent import Agent
from ..checkpoint import describe_error
from ..config import Config
from ..session import list_sessions, save_session
from . import render
from .theme import BEIGE, BG_SELECT, GOLD, MARK, MUTED, SUCCESS, TEXT, WARNING

# /memory resolve 批量策略
_RESOLVE_CHOICES = ["keep_new", "keep_old", "merge"]
_RESOLVE_LABELS = ["全部保留新", "全部保留旧", "全部合并", "跳过"]


@dataclass
class ConfirmRequest:
    """模态确认请求:App 渲染选项列表,用户选择后回调 handle_confirm。"""

    tag: str
    question: str
    choices: list[str] = field(default_factory=list)
    labels: list[str] | None = None  # 与 choices 等长的展示文案
    # 4 选项标签的便捷入口(全部保留新/旧/合并/跳过)


@dataclass
class CommandResult:
    lines: list[Text] = field(default_factory=list)
    action: str = ""                      # "exit" | "reset" | ""
    confirm: ConfirmRequest | None = None


class CommandRunner:
    """一次交互会话共享的命令处理器。"""

    def __init__(self, agent: Agent, config: Config):
        self.agent = agent
        self.config = config

    # ------------------------------------------------------------------ 入口

    def dispatch(self, user_input: str) -> CommandResult:
        line = user_input.strip()
        tokens = line.split()
        cmd = tokens[0].lower() if tokens else ""
        arg = line[len(cmd):].strip()

        table = {
            "/help": self._help, "/h": self._help,
            "/reset": self._reset,
            "/model": self._model,
            "/tokens": self._tokens,
            "/compact": self._compact,
            "/save": self._save,
            "/sessions": self._sessions,
            "/diff": self._diff,
            "/crontab": self._crontab,
            "/memory": self._memory,
            "/task": self._task,
            "/team": self._team,
            "/checkpoint": self._checkpoint, "/cp": self._checkpoint,
            "/quit": self._quit, "/exit": self._quit,
            "quit": self._quit, "exit": self._quit,
        }
        handler = table.get(cmd)
        if handler is None:
            if line.startswith("/"):
                return CommandResult(lines=[Text(f"未知命令: {cmd}(试试 /help)", style=WARNING)])
            # 不是命令也不是退出词 → 交给 agent 当作请求
            return CommandResult()
        return handler(arg, tokens)

    # ------------------------------------------------------------------ 命令

    def _help(self, arg: str, tokens: list[str]) -> CommandResult:
        text = (
            "◆ 命令\n"
            "  /help       帮助\n"
            "  /reset      清空对话历史(保留 .TASK/ 持久任务)\n"
            "  /model      显示当前模型\n"
            "  /model <n>  中途切换模型\n"
            "  /tokens     显示 token 用量与成本\n"
            "  /compact    压缩上下文\n"
            "  /diff       列出本会话修改过的文件\n"
            "  /save       保存会话\n"
            "  /sessions   列出已保存会话\n"
            "  /crontab    定时任务\n"
            "  /memory     记忆列表 / show / forget / organize / resolve / on / off\n"
            "  /task       任务:list / show <id> / update <id> <state|priority> / archive <root_id> / clear\n"
            "  /team       teammate:status / on / off / release <name> / integrate\n"
            "  /checkpoint 断点:list / show <id> / restore <id> / compact\n"
            "  quit        退出\n"
            "\n"
            "◆ 输入\n"
            "  Enter      发送 / 换行按 Ctrl+J 或 Alt+Enter\n"
            "  Ctrl+C     中断当前回合;空闲时退出\n"
        )
        return CommandResult(lines=[render.markdown_to_text(text)])

    def _reset(self, arg: str, tokens: list[str]) -> CommandResult:
        self.agent.reset()
        return CommandResult(lines=[Text("会话已重置。", style=TEXT)])

    def _model(self, arg: str, tokens: list[str]) -> CommandResult:
        if arg:
            self.agent.llm.model = arg
            self.config.model = arg
            return CommandResult(lines=[Text(f"已切换模型: {arg}", style=TEXT)])
        return CommandResult(lines=[Text(f"当前模型: {self.config.model}", style=TEXT)])

    def _tokens(self, arg: str, tokens: list[str]) -> CommandResult:
        p = getattr(self.agent.llm, "total_prompt_tokens", 0)
        c = getattr(self.agent.llm, "total_completion_tokens", 0)
        cost = getattr(self.agent.llm, "estimated_cost", None)
        line = render.tokens_line(p, c, cost)
        return CommandResult(lines=[Text(line, style=TEXT)])

    def _compact(self, arg: str, tokens: list[str]) -> CommandResult:
        from ..context import estimate_tokens

        before = estimate_tokens(self.agent.messages)
        compressed = self.agent.maybe_compress()
        after = estimate_tokens(self.agent.messages)
        if compressed:
            line = f"已压缩: {before} → {after} tokens({len(self.agent.messages)} messages)"
            return CommandResult(lines=[Text(line, style=SUCCESS)])
        line = f"无需压缩({before} tokens,{len(self.agent.messages)} messages)"
        return CommandResult(lines=[Text(line, style=MUTED)])

    def _save(self, arg: str, tokens: list[str]) -> CommandResult:
        sid = save_session(self.agent.messages, self.config.model)
        lines = [
            Text(f"会话已保存: {sid}", style=SUCCESS),
            Text("恢复方式: encoder -r " + sid, style=MUTED),
        ]
        return CommandResult(lines=lines)

    def _sessions(self, arg: str, tokens: list[str]) -> CommandResult:
        sessions = list_sessions()
        if not sessions:
            return CommandResult(lines=[Text("没有已保存的会话。", style=MUTED)])
        lines = [Text(f"已保存会话({len(sessions)}):", style=GOLD)]
        for s in sessions:
            lines.append(Text(f"  {s['id']}  ({s['model']}, {s['saved_at']}) {s['preview']}",
                              style=TEXT))
        return CommandResult(lines=lines)

    def _diff(self, arg: str, tokens: list[str]) -> CommandResult:
        from ..tools.edit import _changed_files

        if not _changed_files:
            return CommandResult(lines=[Text("本会话未修改任何文件。", style=MUTED)])
        lines = [Text(f"本会话修改的文件({len(_changed_files)}):", style=GOLD)]
        for f in sorted(_changed_files):
            lines.append(Text(f"  {f}", style=BEIGE))
        return CommandResult(lines=lines)

    def _crontab(self, arg: str, tokens: list[str]) -> CommandResult:
        parts = (arg or "").split()
        if parts and parts[0].lower() in ("del", "delete", "rm"):
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /crontab delete <task_id>", style=WARNING)])
            tid = parts[1]
            ok = self.agent.scheduler.delete_task(tid)
            return CommandResult(lines=[
                Text(f"已删除定时任务 {tid}" if ok else f"不存在任务 '{tid}'",
                     style=SUCCESS if ok else WARNING)
            ])
        tasks = self.agent.scheduler.list_tasks()
        if not tasks:
            return CommandResult(
                lines=[Text("暂无定时任务。可让 Agent 创建,如"
                            "「每天早上 8 点整理 AI 资讯」", style=MUTED)])
        lines = [Text(f"定时任务({len(tasks)}):", style=GOLD),
                 Text("删除: /crontab delete <task_id>", style=MUTED)]
        for t in tasks:
            last = t.last_fired or "never"
            lines.append(Text(f"  {t.task_id}  {t.time}  ≤上次: {last}", style=BEIGE))
            lines.append(Text(f"      {t.content}", style=MUTED))
        return CommandResult(lines=lines)

    def _memory(self, arg: str, tokens: list[str]) -> CommandResult:
        mem = self.agent.memory
        if mem is None:
            return CommandResult(lines=[Text("记忆系统不可用(启动时 memory_enabled=False)。",
                                             style=WARNING)])

        parts = (arg or "").split(maxsplit=1)
        cmd = parts[0] if parts else ""
        val = parts[1] if len(parts) > 1 else ""

        if cmd == "show":
            if not val:
                return CommandResult(lines=[Text("用法: /memory show <name>", style=WARNING)])
            return CommandResult(lines=[render.markdown_to_text(mem.show(val))])
        if cmd == "forget":
            if not val:
                return CommandResult(lines=[Text("用法: /memory forget <name>", style=WARNING)])
            if mem.forget(val):
                return CommandResult(lines=[Text(f"已忘记 '{val}'(快照保留在 .MEMORY/.snapshots)。",
                                                 style=SUCCESS)])
            return CommandResult(lines=[Text(f"不存在名为 '{val}' 的记忆。", style=WARNING)])
        if cmd == "organize":
            changed = mem.organize(ask=None)
            if not changed:
                return CommandResult(lines=[Text("无需整理。", style=MUTED)])
            return CommandResult(lines=[Text("  调整完成:", style=GOLD)] + [Text(f"  {c}", style=TEXT) for c in changed])
        if cmd == "resolve":
            pending = mem.pending_notices()
            if not pending:
                return CommandResult(lines=[Text("没有待处理的记忆冲突。", style=MUTED)])
            lines = [Text(f"{len(pending)} 条待裁决的记忆冲突,逐条选择新/旧/合并处理:",
                          style="bold " + GOLD)]
            for i, c in enumerate(pending, 1):
                title = c.get("title") or c.get("id") or "?"
                lines.append(Text(f"  [{i}] {title}", style=BEIGE))
                lines.append(Text(f"       旧: {c.get('old_entry', '')}", style=MUTED))
                lines.append(Text(f"       新: {c.get('new_text', '')}", style=MUTED))
            confirm = ConfirmRequest(
                tag="memory_resolve",
                question="对这批冲突采用哪种策略?",
                choices=_RESOLVE_CHOICES + ["skip"],
                labels=_RESOLVE_LABELS,
            )
            return CommandResult(lines=lines, confirm=confirm)
        if cmd in ("on", "off"):
            self.agent.memory_enabled = cmd == "on"
            return CommandResult(lines=[
                Text(f"记忆已{'启用' if cmd == 'on' else '禁用'}(本会话)。", style=TEXT)])
        if not cmd:
            metas = mem.list_meta()
            if not metas:
                hint = ("还没有记忆。告诉 Agent 一些长期偏好,之后用 /memory organize 整理。")
                return CommandResult(lines=[Text(hint, style=MUTED)])
            lines = [Text(f"记忆({len(metas)}):", style=GOLD),
                     Text("查看: /memory show <name>  |  删除: /memory forget <name>", style=MUTED)]
            for m in sorted(metas, key=lambda t: t.updated, reverse=True):
                lines.append(Text(f"  {m.category}/{m.title}  ({m.updated})", style=BEIGE))
                if m.desc:
                    lines.append(Text(f"      {m.desc}", style=MUTED))
            return CommandResult(lines=lines)
        return CommandResult(lines=[
            Text("用法: /memory [show <name>|forget <name>|organize|resolve|on|off]", style=WARNING)])

    def _task(self, arg: str, tokens: list[str]) -> CommandResult:
        tm = self.agent.tasks
        parts = (arg or "").split()

        if parts and parts[0] == "show":
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /task show <task_id>", style=WARNING)])
            task = tm.get(parts[1])
            if task is None:
                return CommandResult(lines=[Text(f"不存在任务 '{parts[1]}'。", style=WARNING)])
            info = (
                f"- id: {task.task_id}\n"
                f"- state: {task.state}(priority: {task.priority})\n"
                f"- agent: {task.agent or '(未分配)'}\n"
                f"- description: {task.description}\n"
                f"- blockedBy: {task.blockedBy or '-'}\n"
                f"- attempts: {task.attempts}\n"
                f"- last_error: {task.last_error or '-'}\n"
                f"- result: {task.result or '-'}"
            )
            return CommandResult(lines=[render.markdown_to_text(info)])
        if parts and parts[0] == "update":
            if len(parts) < 3:
                return CommandResult(lines=[Text("用法: /task update <id> <state|priority>",
                                                 style=WARNING)])
            tid, val = parts[1], parts[2]
            from ..task import PRIORITIES, STATES

            try:
                if val in STATES:
                    task = tm.update(tid, state=val)
                elif val in PRIORITIES:
                    task = tm.update(tid, priority=val)
                else:
                    return CommandResult(lines=[Text(f"'{val}' 不是合法的 state 或 priority。",
                                                     style=WARNING)])
            except (ValueError, KeyError) as e:
                return CommandResult(lines=[Text(str(e), style=WARNING)])
            return CommandResult(lines=[Text(f"{task.task_id} → state={task.state} priority={task.priority}",
                                             style=SUCCESS)])
        if parts and parts[0] == "archive":
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /task archive <root_id>", style=WARNING)])
            try:
                archived = tm.archive(parts[1])
            except (KeyError, RuntimeError) as e:
                return CommandResult(lines=[Text(str(e), style=WARNING)])
            return CommandResult(lines=[Text(f"已归档 {len(archived)} 个任务到 .TASK/done/。",
                                             style=SUCCESS)])
        if parts and parts[0] == "clear":
            return CommandResult(
                lines=[Text("删除 .TASK/ 下所有活跃任务(归档保留)?", style=WARNING)],
                confirm=ConfirmRequest(tag="task_clear", question="确认清除全部活跃任务?",
                                       choices=["yes", "no"], labels=["是,清除", "取消"]))
        tasks = tm.list()
        if not tasks:
            return CommandResult(
                lines=[Text("暂无任务。复杂请求会自动把计划持久化到 .TASK/ 执行;"
                            "未完成的任务会在每次请求开始时提醒。", style=MUTED)])
        lines = [Text(f"任务({len(tasks)}):", style=GOLD)]
        for t in tasks:
            ln = Text(f"  {t.task_id}  {t.state:<11}", style=BEIGE)
            ln.append(t.description, style=TEXT)
            if t.priority != "normal":
                ln.append(f"  priority: {t.priority}", style=MUTED)
            if t.blockedBy:
                ln.append(f"  blockedBy: {t.blockedBy}", style=MUTED)
            if t.last_error:
                ln.append(f"  last_error: {t.last_error}", style=WARNING)
            lines.append(ln)
        return CommandResult(lines=lines)

    def _team(self, arg: str, tokens: list[str]) -> CommandResult:
        agent = self.agent
        parts = (arg or "").split()
        cmd = parts[0] if parts else ""

        if cmd == "on":
            if agent.team is None:
                from ..team import TeamManager

                agent.team = TeamManager(
                    lead=agent, worktrees=agent.team_worktrees, max_teammates=3,
                    team_model=agent.team_model, team_api_key=agent.team_api_key,
                    team_base_url=agent.team_base_url,
                    integration_model=agent.integration_model,
                    integration_api_key=agent.integration_api_key,
                    integration_base_url=agent.integration_base_url,
                )
                agent.team_enabled = True
            iso = "开" if agent.team.worktrees else "关"
            txt = (f"Teammate 模式已开启。你是 Lead;teammate 是常驻协作者"
                   f"(worktree 默认隔离:{iso},代码改动经 integrate_results 归并)。")
            return CommandResult(lines=[Text(txt, style=SUCCESS)])
        if cmd == "off":
            team = agent.team
            if team is not None:
                team.release_all()
            agent.team = None
            agent.team_enabled = False
            return CommandResult(lines=[Text("Teammate 模式已关闭。", style=TEXT)])
        if cmd == "release":
            if agent.team is None:
                return CommandResult(lines=[Text("Teammate 模式未开启。", style=WARNING)])
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /team release <name>", style=WARNING)])
            name = parts[1]
            if agent.team.release(name):
                return CommandResult(lines=[Text(f"已结束 {name}。", style=SUCCESS)])
            return CommandResult(lines=[Text(f"不存在名为 '{name}' 的 teammate。", style=WARNING)])
        if cmd == "integrate":
            if agent.team is None:
                return CommandResult(lines=[Text("Teammate 模式未开启。", style=WARNING)])
            out = agent.team.integrate()
            return CommandResult(lines=[Text(out, style=TEXT)])

        if agent.team is None:
            return CommandResult(lines=[Text("Teammate 模式关闭。开启: /team on(由你决定,"
                                             "Lead 只能建议)。", style=MUTED)])
        statuses = agent.team.status()
        if not statuses:
            return CommandResult(lines=[Text("Teammate 模式开启,暂无队友。", style=MUTED)])
        lines = [Text(f"Teammates({len(statuses)}):", style=GOLD)]
        for s in statuses:
            ln = Text(f"  {s['name']}  {s.get('status')}", style=BEIGE)
            if s.get("task_id"):
                ln.append(f"  (task {s['task_id']})", style=MUTED)
            lines.append(ln)
        return CommandResult(lines=lines)

    def _checkpoint(self, arg: str, tokens: list[str]) -> CommandResult:
        agent = self.agent
        parts = (arg or "").split()
        cmd = parts[0] if parts else "list"

        if agent.checkpoints is None:
            return CommandResult(lines=[Text("Checkpoint 未启用(ENCODER_CHECKPOINT_ENABLED=0)。",
                                             style=WARNING)])

        if cmd in ("list", "ls"):
            metas = agent.checkpoints.list_checkpoints()
            if not metas:
                return CommandResult(lines=[Text("还没有断点。", style=MUTED)])
            head = agent.checkpoints.head_id()
            lines = [Text(f"断点({len(metas)} 个,当前 {head or '-'})"
                          f"  session={agent.checkpoints.session_id}", style=GOLD)]
            for m in metas:
                ln = Text(f" {'→' if m.get('id') == head else ' '} "
                          f"{m.get('id')}  {m.get('created_at', '')}  "
                          f"{m.get('label', '')}", style=BEIGE)
                if m.get("replaces"):
                    ln.append(f"  (合并 {len(m['replaces'])} 个)", style=MUTED)
                resume = m.get("resume") or {}
                if resume:
                    ln.append(f"  ↻{resume.get('kind', 'task')}", style=GOLD)
                lines.append(ln)
            lines.append(Text("↻ = 知道下一步该做什么;restore 之后回复「继续」即可接上。",
                              style=MUTED))
            return CommandResult(lines=lines)

        if cmd == "show":
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /checkpoint show <id>", style=WARNING)])
            if not agent.checkpoints.adopt(parts[1]):
                return CommandResult(lines=[Text(f"未找到断点 {parts[1]}", style=WARNING)])
            cp = agent.checkpoints.load(parts[1])
            if cp is None:
                return CommandResult(lines=[Text(f"断点 {parts[1]} 读取失败。", style=WARNING)])
            return CommandResult(lines=self._render_checkpoint(cp))

        if cmd == "restore":
            if len(parts) < 2:
                return CommandResult(lines=[Text("用法: /checkpoint restore <id>", style=WARNING)])
            ok, message = agent.restore_state(parts[1])
            if not ok:
                return CommandResult(lines=[Text(message, style=WARNING)])
            return CommandResult(lines=[
                Text(f"已恢复到断点 {parts[1]}", style=SUCCESS),
                Text(message, style=MUTED),
                Text("恢复的是 agent 状态(messages/todos);想接着原任务做就直接回复「继续」。",
                     style=MUTED),
            ])

        if cmd == "compact":
            return CommandResult(lines=[Text(agent.checkpoints.compact(), style=TEXT)])

        return CommandResult(lines=[Text("用法: /checkpoint [list|show <id>|restore <id>|compact]",
                                         style=WARNING)])

    def _render_checkpoint(self, cp: dict) -> list[Text]:
        """一个断点里到底有什么(只报状态;文件回退归 git)。"""
        meta, state = cp.get("meta", {}), cp.get("state", {})
        agent_state, execu = state.get("agent", {}), state.get("execution", {})
        ctx = agent_state.get("context", {})
        lines = [Text(f"{meta.get('id')}  {meta.get('created_at')}  "
                      f"触发={meta.get('trigger')}  {meta.get('label', '')}", style=GOLD)]
        if meta.get("parent_id"):
            lines.append(Text(f"  parent: {meta['parent_id']}", style=MUTED))
        lines.append(Text(f"  messages: {len(agent_state.get('messages', []))} 条,"
                          f"约 {ctx.get('token_estimate', 0)} tokens", style=TEXT))
        for t in (agent_state.get("todos") or [])[:6]:
            lines.append(Text(f"  todo {t.get('todo_id')}[{t.get('status')}] "
                              f"{t.get('title', '')}", style=BEIGE))
        for t in (agent_state.get("tasks") or [])[:6]:
            lines.append(Text(f"  task {t.get('task_id')}[{t.get('state')}] "
                              f"{t.get('description', '')}", style=BEIGE))
        files = ctx.get("files") or []
        if files:
            lines.append(Text(f"  改过的文件: {', '.join(files[:8])}", style=MUTED))
        for r in execu.get("running") or []:
            lines.append(Text(f"  队友 {r.get('name')}({r.get('status')})", style=MUTED))
        if execu.get("pending_approval"):
            approval = execu["pending_approval"]
            lines.append(Text(f"  待批准: {approval.get('command') or '(无命令)'}"
                              f"({approval.get('reason') or approval.get('tool', '')})",
                              style=WARNING))
        if execu.get("pending_tool_call"):
            call = execu["pending_tool_call"]
            lines.append(Text(f"  未答完的调用: {call.get('tool')}({call.get('args')})",
                              style=WARNING))
        if execu.get("last_error"):
            lines.append(Text(f"  上次为什么停下: {describe_error(execu['last_error'])}",
                              style=WARNING))
        if (execu.get("resume") or {}).get("kind") == "review":
            lines.append(Text("  这是 review 任务:恢复后接着执行,别重新派队友", style=GOLD))
        cp_env = state.get("env", {})
        lines.append(Text(f"  env: cwd={cp_env.get('cwd')} branch={cp_env.get('branch') or '-'} "
                          f"HEAD={cp_env.get('git_head') or '-'}", style=MUTED))
        for c in self.agent.checkpoints.env_diff(cp_env):
            lines.append(Text(f"  ⚠️ {c}", style=WARNING))
        lines.append(Text("  (只是 agent 状态;文件回退请用 git)", style=MUTED))
        return lines

    def _quit(self, arg: str, tokens: list[str]) -> CommandResult:
        return CommandResult(action="exit")

    # ------------------------------------------------------------------ 确认回调

    def handle_confirm(self, req: ConfirmRequest, choice: str) -> CommandResult:
        """App 在模态里收到用户选择后回调。"""
        if req.tag == "task_clear":
            if choice == "yes":
                self.agent.tasks.clear()
                return CommandResult(lines=[Text("已清除全部活跃任务。", style=SUCCESS)])
            return CommandResult(lines=[Text("已取消。", style=MUTED)])
        if req.tag == "memory_resolve":
            if choice == "skip":
                return CommandResult(lines=[Text("已跳过。", style=MUTED)])
            changed = self.agent.memory.organize(ask=lambda question, options: choice)
            if changed:
                lines = [Text("处理完成:", style=GOLD)]
                lines += [Text(f"  {c}", style=TEXT) for c in changed]
                return CommandResult(lines=lines)
            return CommandResult(lines=[Text("没有可处理的冲突。", style=MUTED)])
        return CommandResult(lines=[Text("未知确认请求。", style=WARNING)])

    # ------------------------------------------------------------------ 启动卡

    def welcome(self) -> Text:
        """黄金欢迎卡:对齐样式图左侧主区顶部的金色横幅。"""
        model = self.config.model
        base = f"  Base: {self.config.base_url}" if self.config.base_url else ""
        return Text.assemble(
            (f"{MARK} EnCoder v{__version__}", f"bold {GOLD}"),
            (f"\n  Model: {model}{base}\n", TEXT),
            ("  输入你的想法即可;斜杠命令见 /help。\n", MUTED),
            ("  Enter 发送 · Ctrl+J 换行 · Ctrl+C 中断本回合", MUTED),
            style=f"on {BG_SELECT}",
        )