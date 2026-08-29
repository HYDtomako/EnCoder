"""TUI 组件:标题栏 / 对话区 / 状态侧栏 / 输入条 / 底部状态栏 / 确认模态。

配色全部来自 :mod:`.theme`(样式图提取),布局与样式图一一对应:
标题栏(暗底亮字)→ 对话主区 + 右侧状态栏(金色状态卡 + 分节列表)→ 输入条 → 底部快捷键提示。
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from ..agent import Agent
from . import render
from .theme import (
    BEIGE,
    BG_SELECT,
    DIM,
    GEAR,
    GOLD,
    IDLE,
    MARK,
    MUTED,
    RUNNING,
    SUCCESS,
    TEXT,
)

# ===================================================================== 标题栏

class HeaderTitle(Static):
    """顶栏左:``◆ EnCoder v0.4.0`` + TUI 徽标。"""


class HeaderModel(Static):
    """顶栏右:模型名(+ base,弱化)。"""


class HeaderBar(Horizontal):
    """标题栏(Horizontal + CSS:左 dock、右 dock)。"""


# ===================================================================== 对话区

class Conversation(RichLog):
    """消息区:所有内容以 rich.text.Text 写入,markup 关闭防止消息正文注入样式。"""

    def __init__(self) -> None:
        super().__init__(
            markup=False,
            highlight=False,
            wrap=False,
            auto_scroll=True,
            max_lines=5000,
        )

    def add_user(self, text: str) -> None:
        self.write(render.chat_bubble_text("user", text))

    def add_agent(self, text: str) -> None:
        head = Text(f"\n{MARK} ", style=GOLD)
        self.write(head + render.markdown_to_text(text))

    def add_tool(self, name: str, brief: str) -> None:
        line = Text(f"  {GEAR} ", style=DIM) + Text(f"{name}({brief})", style=DIM)
        self.write(line)

    def add_system(self, text: str, style: str = MUTED) -> None:
        self.write(Text(text, style=style))

    def add_stream(self, chunk: str) -> None:
        self.write(Text(chunk, style=TEXT))


# ===================================================================== 侧栏

class Sidebar(Static):
    """右侧状态栏:金色状态卡 + TASKS / MEMORY / TEAM / CRON 分节列表。"""

    def refresh_agent(self, agent: Agent, busy: bool) -> None:
        self.update(build_sidebar(agent, busy))


def build_sidebar(agent: Agent, busy: bool) -> Text:
    """从 agent 读取所有运行时状态,渲染成侧栏内容(错误安全,不抛异常)。"""
    body = Text()
    status = RUNNING if busy else IDLE

    # ---- 金色状态卡(对齐样式图侧栏顶部金色卡片) ----
    card = Text()
    card.append(f"  ◆ {status}\n", style=f"bold {GOLD}")
    card.append(f"  {agent.llm.model}\n", style=TEXT)
    # 真实 LLM 有 token 计数;ScriptedLLM 等离线实现可能没有 → 缺省兜底
    p = getattr(agent.llm, "total_prompt_tokens", 0)
    c = getattr(agent.llm, "total_completion_tokens", 0)
    cost = getattr(agent.llm, "estimated_cost", None)
    card.append(f"  {render.tokens_line(p, c, cost)}\n", style=MUTED)
    mem_on = getattr(agent, "memory", None) is not None and agent.memory_enabled
    card.append(f"  记忆: {'ON' if mem_on else 'OFF'}", style=SUCCESS if mem_on else MUTED)
    # 卡片整体叠加金色底:嵌入 body 时基样式会丢,所以用覆盖全卡的 span
    card.stylize(f"on {BG_SELECT}")
    body.append(card)
    body.append("\n")

    # ---- TODO(会话级清单) ----
    todos = _guarded(lambda: agent.todos.list())
    if todos:
        body.append("    TODO\n", style=f"bold {GOLD}")
        for t in todos:
            dot = {"pending": "○", "in_progress": "●", "completed": "✓"}.get(t.status, "○")
            color = GOLD if t.status == "in_progress" else (
                SUCCESS if t.status == "completed" else MUTED)
            body.append(f"  {dot} {t.title}\n", style=color)
        body.append("\n")

    # ---- TASKS(.TASK 持久任务) ----
    tasks = _guarded(agent.tasks.list)
    if tasks:
        body.append("    TASKS\n", style=f"bold {GOLD}")
        for t in tasks:
            body.append(f"  {t.state:<12}", style=MUTED)
            body.append(t.description[:40], style=TEXT)
            if t.blockedBy:
                body.append(f" (⛓ {t.blockedBy})", style=MUTED)
            body.append("\n")
        body.append("\n")

    # ---- MEMORY ----
    metas = _guarded(_list_meta, agent)
    if metas:
        body.append("    MEMORY\n", style=f"bold {GOLD}")
        for m in sorted(metas, key=lambda x: x.updated, reverse=True)[:6]:
            body.append(f"  {m.category}/{m.title}\n", style=BEIGE)
        body.append("\n")

    # ---- TEAM ----
    team_lines = _guarded(_team_status, agent)
    body.append("    TEAM\n", style=f"bold {GOLD}")
    if team_lines:
        for s in team_lines:
            line = f"  {s.get('name', '?')}  {s.get('status', '?')}"
            if s.get("task_id"):
                line += f" (task {s['task_id']})"
            body.append(line + "\n", style=BEIGE)
    else:
        body.append("  (关)\n", style=MUTED)

    # ---- CRON ----
    crons = _guarded(agent.scheduler.list_tasks)
    if crons:
        body.append("    CRON\n", style=f"bold {GOLD}")
        for t in crons:
            body.append(f"  {t.time}  {t.content[:22]}\n", style=BEIGE)
    return body


def _guarded(fn, *args):
    try:
        return list(fn(*args)) if args else list(fn())
    except Exception:  # noqa: BLE001 - 侧栏渲染错误安全:任何异常都当作「无数据」
        return []


def _list_meta(agent: Agent):
    mem = getattr(agent, "memory", None)
    return mem.list_meta() if mem is not None else []


def _team_status(agent: Agent):
    if getattr(agent, "team", None) is None:
        return []
    return agent.team.status()


# ===================================================================== 输入条

class InputPane(TextArea):
    """消息输入框:Enter 发送,Ctrl+J / Alt+Enter 换行。"""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "submit", "发送", priority=True),
        Binding("ctrl+j", "newline", "换行", priority=True),
        Binding("alt+enter", "newline", "换行", priority=True),
    ]

    class Submitted(Message):
        """输入框里提交了一条消息。"""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def action_submit(self) -> None:
        text = self.text
        if not text.strip():
            return
        self.text = ""
        self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")


# ===================================================================== 底部栏

class FootBar(Static):
    """底部状态行:快捷键提示 + 运行状态。"""

    def set_status(self, busy: bool, note: str = "") -> None:
        flag = Text(
            f"  {RUNNING}  " if busy else f"  {IDLE}  ",
            style=f"bold {GOLD}" if busy else MUTED,
        )
        text = Text.assemble(
            flag,
            ("Enter 发送 · Ctrl+J 换行 · Ctrl+C 中断/退出 · /help", MUTED),
            ("      " + note, DIM),
        )
        self.update(text)


# ===================================================================== 确认模态

class ChoiceModal(ModalScreen):
    """选项列表模态:``push_screen(screen, callback)`` 后,选中项通过 dismiss(值) 回传。"""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "取消", priority=True),
    ]

    def __init__(
        self,
        question: str,
        options: list[str],
        values: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._question = question
        self._options = options
        self._values = values or options

    def compose(self) -> ComposeResult:
        yield Static(f"{self._question}\n按 ↑/↓ 选择,Enter 确认,Esc 取消", classes="modal-question")
        yield OptionList(*[Option(f"{i + 1}) {label}") for i, label in enumerate(self._options)])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._values[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(self._values[-1] if self._values else None)