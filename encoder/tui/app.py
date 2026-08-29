"""EnCoder 全屏 TUI —— 基于 ``tui_image/样式.png`` 的「暖琥珀金 × 近黑炭灰」主题。

架构(核心 Agent 零改动):
- ``AgentBridge`` 在工作线程里串行执行 ``agent.chat``,回调事件经线程安全队列传回;
- 本 App 用 ``set_interval`` 在主线程定时 drain 事件队列并渲染(RichLog/Static/TextArea
  的变更全部发生在主线程,跨线程只调用线程安全的 put/queue);
- 定时任务复用现有 Scheduler:1.5s 轮询,空闲时把到期任务交给 Bridge 执行,不打断当前回合;
- 退出时释放 teammate 与 scheduler,与 ``cli._repl`` 语义一致。
"""

from __future__ import annotations

import queue
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static

from ..agent import Agent
from ..config import Config
from ..cron_scheduler import ScheduleTask
from . import render
from .bridge import AgentBridge, Job
from .commands import CommandResult, CommandRunner, ConfirmRequest
from .theme import BEIGE, BG, BG_RAISED, GOLD, HAIRLINE, MUTED, TEXT
from .widgets import (
    ChoiceModal,
    Conversation,
    FootBar,
    HeaderBar,
    HeaderModel,
    HeaderTitle,
    InputPane,
    Sidebar,
)


class EncoderTuiApp(App[None]):
    """主 App:装配布局、按键、Bridge 事件消费与定时任务轮询。"""

    TITLE = "EnCoder TUI"
    SUB_TITLE = "agent console"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "ctrl_c", "中断 / 退出", priority=True),
    ]

    CSS = f"""
        Screen {{
            layout: vertical;
            background: {BG};
            color: {TEXT};
        }}
        #header {{
            height: 1;
            background: #000000;
            color: {TEXT};
        }}
        #header HeaderTitle {{ dock: left;  width: auto; padding: 0 0 0 1; }}
        #header HeaderModel {{ dock: right; width: auto; padding: 0 1 0 0; }}
        #body {{ height: 1fr; background: {BG}; }}
        Conversation {{
            width: 3fr;
            border-right: solid {HAIRLINE};
            background: {BG};
            padding: 0 1 0 1;
        }}
        Sidebar {{ width: 1fr; background: {BG}; padding: 0 1 0 1; }}
        #inputrow {{ height: 4; background: {BG_RAISED}; }}
        #prompt {{
            dock: left;
            width: auto;
            padding: 0 0 0 1;
            color: {BEIGE};
        }}
        InputPane {{
            background: {BG_RAISED};
            color: {TEXT};
            border: none;
            padding: 0 1 0 0;
        }}
        FootBar {{ height: 1; background: #000000; color: {MUTED}; }}
        ChoiceModal {{ align: center middle; background: $boost; }}
        ChoiceModal > .dialog {{ width: 64; height: auto; background: {BG_RAISED};
                                 border: tall {HAIRLINE}; padding: 1 2 1 2; }}
        ChoiceModal OptionList {{ height: auto; max-height: 12; background: {BG_RAISED};
                                  border: tall {HAIRLINE}; color: {TEXT}; }}
        ChoiceModal OptionList:focus {{ border: tall {GOLD}; }}
    """

    def __init__(self, agent: Agent, config: Config, demo=False) -> None:
        super().__init__()
        self.agent = agent
        self.config = config
        self.demo = demo
        self.bridge = AgentBridge(agent)
        self.runner = CommandRunner(agent, config)
        self._busy = False
        self._active_job: Job | None = None
        self._streamed_this_round = False
        self._input: InputPane | None = None

    # ------------------------------------------------------------------ 组装

    def compose(self) -> ComposeResult:
        with HeaderBar(id="header"):
            yield HeaderTitle(
                Text.assemble(("◆ EnCoder ", f"bold {GOLD}"), ("TUI", MUTED)), id="appname"
            )
            yield HeaderModel(self._model_text(), id="model")
        with Horizontal(id="body"):
            yield Conversation()
            yield Sidebar()
        with Horizontal(id="inputrow"):
            yield Static("You> ", id="prompt")
            yield InputPane(id="inputpane")
        yield FootBar()

    def on_mount(self) -> None:
        self.bridge.start()
        self.agent.scheduler.start()

        conv = self.query_one(Conversation)
        conv.write(self.runner.welcome())
        conv.write(Text(""))

        self._input = self.query_one(InputPane)
        self._input.focus()

        self.set_interval(0.12, self._drain_events, name="drain")
        self.set_interval(1.5, self._tick_cron, name="cron")
        self.query_one(FootBar).set_status(False)

        if self.demo:
            self.call_after_refresh(self._start_demo)

    def _model_text(self) -> Text:
        t = Text(f"  {self.agent.llm.model}", style=BEIGE)
        if self.config.base_url:
            t.append(f" · {self.config.base_url}", style=MUTED)
        return t

    def _start_demo(self) -> None:
        # 离线脚本化演示:ScriptedLLM 剧本自动播放
        from ..demo import _TASK

        self._submit_user(_TASK)

    # ------------------------------------------------------------------ 按键

    def action_ctrl_c(self) -> None:
        focused = self.focused
        if isinstance(focused, InputPane):
            sel = getattr(focused, "selected_text", "") or ""
            if sel:
                self.copy_to_clipboard(sel)
                return
        if self._busy:
            self.bridge.request_cancel()
            return
        self._teardown()
        self.exit(return_code=0, message="")

    # ------------------------------------------------------------------ 输入

    def on_input_pane_submitted(self, message: InputPane.Submitted) -> None:
        self._handle_typed(message.text)

    def _handle_typed(self, text: str) -> None:
        text = text.strip()
        if not text or self._busy:
            return
        if text.lower() in ("quit", "exit", "/quit", "/exit"):
            self._teardown()
            self.exit(return_code=0, message="")
            return
        if text.startswith("/"):
            self._apply_command_result(self.runner.dispatch(text))
            return
        self._submit_user(text)

    def _apply_command_result(self, result: CommandResult) -> None:
        conv = self.query_one(Conversation)
        for line in result.lines:
            conv.write(line)
        if result.confirm:
            self._ask_confirm(result.confirm)
        if result.action == "exit":
            self._teardown()
            self.exit(return_code=0, message="")

    def _ask_confirm(self, req: ConfirmRequest) -> None:
        labels = req.labels or req.choices
        screen = ChoiceModal(req.question, list(labels), req.choices)
        self.push_screen(screen, lambda choice: self._on_confirm(req, choice))

    def _on_confirm(self, req: ConfirmRequest, choice: str | None) -> None:
        if choice is None:
            return
        self._apply_command_result(self.runner.handle_confirm(req, choice))

    def _submit_user(self, text: str) -> None:
        self.query_one(Conversation).add_user(text)
        job = self.bridge.submit(text, kind="user")
        self._active_job = job
        self._streamed_this_round = False
        self._set_busy(True)

    # ------------------------------------------------------------------ 定时任务

    def _tick_cron(self) -> None:
        """空闲时排空 scheduler.queue;触发任务不会打断当前回合。"""
        if self._busy:
            return
        while True:
            try:
                task: ScheduleTask = self.agent.scheduler.queue.get_nowait()
            except queue.Empty:
                break
            self._fire_cron(task)

    def _fire_cron(self, task: ScheduleTask) -> None:
        self.query_one(Conversation).add_system(
            f"⚡ 定时任务触发: {task.time}  {task.content}", style=GOLD
        )
        job = self.bridge.submit(task.content, kind="cron", extra=task)
        self._active_job = job
        self._streamed_this_round = False
        self._set_busy(True)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one(Sidebar).refresh_agent(self.agent, busy)
        self.query_one(FootBar).set_status(busy, "")

    def _drain_events(self) -> None:
        events = self.bridge.drain()
        if not events:
            return
        conv = self.query_one(Conversation)
        for event in events:
            kind, payload = event[0], event[1:]
            try:
                if kind == "tokens":
                    self._streamed_this_round = True
                    conv.add_stream(payload[0])
                elif kind == "tool":
                    name, call = payload
                    conv.add_tool(name, render.tool_brief(call or {}))
                elif kind == "ended":
                    self._on_round_finished("ok", payload[0])
                    if payload[0] and not self._streamed_this_round:
                        conv.add_agent(payload[0])
                elif kind == "cancelled":
                    self._on_round_finished("cancelled", "")
                    conv.add_system("⏹ 已中断本回合(历史已回滚)。", style=MUTED)
                elif kind == "error":
                    self._on_round_finished("error", "")
                    conv.add_system(f"✖ {payload[0]}", style="#d07a5e")
                elif kind == "round_done":
                    self._on_round_finished("done", "")
            except Exception:  # noqa: S112 BLE001 - 单个事件渲染失败不应拖垮 drain 循环
                continue

    def _on_round_finished(self, outcome: str, response: str) -> None:
        """回合结束统一处理:记录 cron 日志、刷新状态并清空活跃作业。"""
        job = self._active_job
        if job is not None and job.kind == "cron":
            self._log_cron_result(job, response)
        self._busy = False
        self._streamed_this_round = False
        self._active_job = None
        self.query_one(FootBar).set_status(False)
        self.query_one(Sidebar).refresh_agent(self.agent, False)

    def _log_cron_result(self, job: Job, response: str) -> None:
        """定时任务执行结果写回 ~/.encoder/tasks.log(与 cli 一致)。"""
        task = job.extra
        if task is None:
            return
        try:
            import time as _time
            from pathlib import Path

            log = Path.home() / ".encoder" / "tasks.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            stamp = _time.strftime("%Y-%m-%d %H:%M:%S")
            payload = f"[{stamp}] {task.task_id} {task.time} {task.content}\n"
            if response:
                payload += response.strip() + "\n"
            payload += "=" * 60 + "\n"
            with log.open("a", encoding="utf-8") as f:
                f.write(payload)
        except Exception:  # noqa: S110 BLE001 - 日志写入失败不可见即可
            pass

    # ------------------------------------------------------------------ 退出

    def _teardown(self) -> None:
        """退出清理:停 Bridge 与 scheduler、释放 teammate。

        注意:不能叫 ``_shutdown`` —— Textual 内部也有 ``App._shutdown``(async,
        run()/run_test() 退出时会 await),重名会破坏 Textual 自身的清理。
        """
        for closer in (
            lambda: self.bridge.stop(),
            lambda: self.agent.scheduler.stop(),
            lambda: getattr(self.agent, "team", None).release_all()
            if getattr(self.agent, "team", None) is not None else None,
        ):
            try:
                closer()
            except Exception:  # noqa: S110 BLE001 - 退出清理不因单个失败而中断
                pass


def run_tui(agent: Agent, config: Config, demo: bool = False) -> int:
    """以 TUI 作为交互层启动。进入前 agent 已构建完毕;正常退出返回 0。"""
    app = EncoderTuiApp(agent, config, demo=demo)
    app.run()
    return 0