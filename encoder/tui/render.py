"""渲染纯函数:把消息、工具调用、token 统计变成带样式的 ``rich.text.Text`` 或描述字符串。

本模块**不 import textual**,只依赖 rich(项目已有依赖),便于在 headless 环境下单测。
样式中只使用 ``tui/theme.py`` 里提取的调色板色值。
"""

from __future__ import annotations

import re

from rich.text import Text

from .theme import BEIGE, BG_RAISED, CARET, DIM, GOLD, MUTED, TEXT

# 内联 markdown token:**加粗** / *斜体* / `行内代码`
_INLINE_RE = re.compile(
    r"\*\*([^*]+)\*\*"      # **bold**
    r"|`([^`]+)`"           # `code`
    r"|(?<!\*)\*([^*\s][^*]*?)\*(?!\*)"  # *italic*
)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*$")


def escape_markup(text: str) -> str:
    """把可能破坏 rich markup 的 ``[`` ``]`` 转义,用于静态文案拼接。

    富文本推荐直接用 :func:`inline_marks` / :func:`markdown_to_text` 构造 Text,
    无需转义;此函数仅服务少数需要写 rich markup 字符串的场景。
    """
    return text.replace("[", "\\[").replace("]", "\\]")


def inline_marks(text: str) -> Text:
    """浅内联 markdown → rich Text:**bold**,*italic*,`code`。"""
    out = Text()
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos : m.start()])
        bold, code, ital = m.groups()
        if bold is not None:
            out.append(bold, style="bold")
        elif code is not None:
            out.append(f"`{code}`", style=f"bold {GOLD}")
        else:  # italic
            out.append(ital, style="italic")
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def markdown_to_text(source: str) -> Text:
    """完整的消息文本 → 带样式的 Text。

    处理顺序:
    1. 行级状态机拆分 ``` 围栏代码块(全块弱化为米灰、背景抬升);
    2. 标题行(``#`` → 金色粗体);
    3. 其余行交给 :func:`inline_marks` 做内联加粗/斜体/行内代码。
    """
    out = Text()
    code_buf: list[str] = []
    in_code = False

    def flush():
        nonlocal code_buf
        if code_buf:
            block = "\n".join(code_buf)
            out.append(Text(block, style=f"{MUTED} on {BG_RAISED}"))
            out.append("\n")
            code_buf = []

    for raw in source.splitlines():
        stripped = raw.strip()
        if _FENCE_RE.match(stripped):
            if in_code:
                flush()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue
        hm = _HEADING_RE.match(raw)
        if hm:
            out.append(Text(hm.group(2).strip(), style=f"bold {GOLD}"))
            out.append("\n")
            continue
        out.append(inline_marks(raw))
        out.append("\n")
    flush()
    if in_code:  # 未闭合的围栏按代码块收尾,避免样式漏色
        block = "\n".join(code_buf)
        out.append(Text(block, style=f"{MUTED} on {BG_RAISED}"))
        out.append("\n")
    return out


def tool_brief(kwargs: dict, maxlen: int = 80) -> str:
    """工具调用参数的简要摘要(逻辑对齐 ``cli._brief``,裁剪到 maxlen)。"""
    parts = []
    for key, value in kwargs.items():
        r = str(value)
        if len(r) > 40:
            r = r[:37] + "..."
        parts.append(f"{key}={r}")
    s = ", ".join(parts)
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


def tokens_line(prompt: int, completion: int, cost: float | None) -> str:
    """token 统计摘要:``123 prompt · 45 completion · 168 total · ~$0.0002``。"""
    total = prompt + completion
    line = f"{prompt} prompt · {completion} completion · {total} total"
    if cost is not None:
        line += f" · ~${cost:.4f}"
    return line


def chat_bubble_text(kind: str, body: str) -> Text:
    """把一段对话内容包装成带前缀的 Text。

    - ``user``: 米白前缀金标 ``┃``
    - ``agent``: 金色菱形 ``◆`` + 正文亮灰
    - ``tool``: 暗色齿轮 ``⚙ ...``
    - ``system``: 弱化文本,无前缀
    """
    if kind == "user":
        t = Text()
        t.append(CARET + " ", style=GOLD)
        t.append(body, style=BEIGE)
        return t
    if kind == "agent":
        t = Text()
        t.append("◆ ", style=GOLD)
        t.append(body, style=TEXT)
        return t
    if kind == "tool":
        t = Text()
        t.append("  ⚙ ", style=DIM)
        t.append(body, style=DIM)
        return t
    return Text(body, style=MUTED)