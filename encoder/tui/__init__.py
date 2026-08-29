"""EnCoder TUI 交互层 —— 以 ``encoder --tui`` 启用的全屏 Agent 控制台。

设计依据 ``tui_image/样式.png``(暖琥珀金 × 近黑炭灰),架构上只做新交互层,
不改任何核心 Agent 能力。
"""

from .app import run_tui

__all__ = ["run_tui"]