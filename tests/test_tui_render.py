"""Headless tests for the TUI layer's no-textual modules.

Covers ``encoder.tui.render`` / ``encoder.tui.theme`` (pure rendering functions)
and ``encoder.tui.commands`` (CommandRunner driven by a stubbed agent).
Nothing here imports ``textual``, so it runs in a normal terminal / CI.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text

from encoder.tui import render
from encoder.tui.commands import CommandRunner
from encoder.tui.theme import (
    BEIGE,
    BG_RAISED,
    CARET,
    GOLD,
    MUTED,
    PROMPT,
    TEXT,
)


def _styled(t: Text) -> list[tuple[str, str]]:
    """把 Text 的 spans 变成 [(文本段, 样式), ...],便于断言。"""
    return [(t.plain[s.start:s.end], s.style) for s in t.spans]


# ===================================================================== theme

@pytest.mark.parametrize("color", [BEIGE, BG_RAISED, GOLD, MUTED, TEXT])
def test_theme_hex_format(color: str):
    assert len(color) == 7 and color.startswith("#")

def test_theme_symbols():
    assert CARET == "┃"
    assert PROMPT == "You> "


# ===================================================================== render

def test_escape_markup():
    assert render.escape_markup("[正在处理]") == "\\[正在处理\\]"


def test_inline_marks_bold():
    t = render.inline_marks("看 **这里** 结束")
    assert t.plain == "看 这里 结束"
    assert ("这里", "bold") in _styled(t)


def test_inline_marks_code():
    t = render.inline_marks("用 `git log` 查看")
    assert "`git log`" in t.plain
    assert ("`git log`", f"bold {GOLD}") in _styled(t)


def test_inline_marks_italic():
    t = render.inline_marks("*稍等* 一下")
    assert ("稍等", "italic") in _styled(t)


def test_markdown_heading_is_gold():
    t = render.markdown_to_text("# 任务列表")
    assert ("任务列表", f"bold {GOLD}") in _styled(t)


def test_markdown_fenced_code_is_raised():
    t = render.markdown_to_text("开头\n```py\nprint(1)\n```\n结尾")
    assert "print(1)" in t.plain
    raised = [s.style for s in t.spans if s.style and f"on {BG_RAISED}" in s.style]
    assert raised


def test_markdown_unclosed_fence_recovers():
    t = render.markdown_to_text("```\n残片")
    assert "残片" in t.plain  # 不抛异常,围栏未闭合也照常收尾


def test_tool_brief_truncates_long_value():
    brief = render.tool_brief({"file": "x" * 60, "content": "y"})
    assert brief.startswith("file=")
    assert "..." in brief
    assert len(brief) <= 80 + 3  # maxlen + 省略号


def test_tool_brief_len_cap():
    brief = render.tool_brief({"a": "x", "b": "y" * 50}, maxlen=20)
    assert len(brief) <= 23


def test_tokens_line_with_cost():
    line = render.tokens_line(10, 5, 0.001)
    assert line == "10 prompt · 5 completion · 15 total · ~$0.0010"


def test_tokens_line_without_cost():
    assert render.tokens_line(1, 2, None) == "1 prompt · 2 completion · 3 total"


@pytest.mark.parametrize("kind,prefix", [
    ("user", "┃ "),
    ("agent", "◆ "),
    ("tool", "  ⚙ "),
])
def test_chat_bubble_prefix(kind: str, prefix: str):
    t = render.chat_bubble_text(kind, "正文")
    assert t.plain == prefix + "正文"


def test_chat_bubble_user_style():
    t = render.chat_bubble_text("user", "hi")
    assert _styled(t) == [("┃ ", GOLD), ("hi", BEIGE)]


def test_chat_bubble_system_default():
    t = render.chat_bubble_text("system", "note")
    assert t.plain == "note"
    assert all(s.style == MUTED for s in t.spans)


# ===================================================================== commands

def _stub_agent(**overrides) -> MagicMock:
    """一个只挂必要属性的假 agent(自动补全其余属性)。"""
    agent = MagicMock()
    agent.llm.model = "test-model"
    agent.llm.total_prompt_tokens = 10
    agent.llm.total_completion_tokens = 5
    agent.llm.estimated_cost = 0.001
    agent.messages = []
    agent.context.maybe_compress.return_value = False
    agent.scheduler.list_tasks.return_value = []
    agent.tasks.list.return_value = []
    agent.memory = MagicMock()
    agent.team = None
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


def _runner(agent=None) -> tuple[CommandRunner, SimpleNamespace]:
    agent = agent or _stub_agent()
    config = SimpleNamespace(model="test-model", base_url="")
    return CommandRunner(agent, config), config


def _plain(result) -> str:
    return "".join(t.plain for t in result.lines)


def test_dispatch_plain_request_returns_empty():
    r, _ = _runner()
    assert r.dispatch("帮我写个函数").lines == []


def test_unknown_command_warns():
    r, _ = _runner()
    out = _plain(r.dispatch("/nope"))
    assert "未知命令" in out


def test_command_quit_exits():
    r, _ = _runner()
    assert r.dispatch("quit").action == "exit"
    assert r.dispatch("/quit").action == "exit"


def test_model_query_and_switch():
    r, config = _runner()
    assert "test-model" in _plain(r.dispatch("/model"))
    r.dispatch("/model gpt-4.1")
    assert r.agent.llm.model == "gpt-4.1"
    assert config.model == "gpt-4.1"


def test_tokens_command():
    r, _ = _runner()
    assert "10 prompt" in _plain(r.dispatch("/tokens"))


def test_compact_noop():
    r, _ = _runner()
    assert "无需压缩" in _plain(r.dispatch("/compact"))


def test_save_session():
    r, _ = _runner()
    with patch("encoder.tui.commands.save_session", return_value="sid-abc") as m:
        out = _plain(r.dispatch("/save"))
    m.assert_called_once()
    assert "sid-abc" in out


def test_sessions_empty():
    r, _ = _runner()
    with patch("encoder.tui.commands.list_sessions", return_value=[]):
        assert "没有已保存" in _plain(r.dispatch("/sessions"))


def test_diff_lists_changed_files():
    r, _ = _runner()
    with patch("encoder.tools.edit._changed_files", {"a.txt", "b.txt"}):
        out = _plain(r.dispatch("/diff"))
    assert "a.txt" in out and "b.txt" in out


def test_crontab_empty_and_delete():
    r, _ = _runner()
    assert "暂无定时任务" in _plain(r.dispatch("/crontab"))
    r.agent.scheduler.delete_task.return_value = True
    assert "已删除" in _plain(r.dispatch("/crontab delete t1"))


def test_memory_disabled():
    r, _ = _runner(_stub_agent(memory=None))
    assert "记忆系统不可用" in _plain(r.dispatch("/memory"))


def test_task_empty():
    r, _ = _runner()
    assert "暂无任务" in _plain(r.dispatch("/task"))


def test_task_clear_asks_confirmation():
    r, _ = _runner()
    res = r.dispatch("/task clear")
    assert res.confirm is not None and res.confirm.tag == "task_clear"


def test_handle_confirm_task_clear_yes():
    r, _ = _runner(_stub_agent())
    res = r.dispatch("/task clear")
    out = _plain(r.handle_confirm(res.confirm, "yes"))
    assert "已清除" in out
    r.agent.tasks.clear.assert_called_once()


def test_handle_confirm_task_clear_no():
    r, _ = _runner(_stub_agent())
    res = r.dispatch("/task clear")
    out = _plain(r.handle_confirm(res.confirm, "no"))
    assert "已取消" in out
    r.agent.tasks.clear.assert_not_called()


def test_team_off_warns():
    r, _ = _runner()
    assert "关闭" in _plain(r.dispatch("/team"))


def test_team_on_creates_manager():
    r, _ = _runner()
    agent = r.agent
    with patch("encoder.team.TeamManager") as tm:
        out = _plain(r.dispatch("/team on"))
    assert "已开启" in out
    assert agent.team is tm.return_value  # 持有同一个 team 生命周期


def test_memory_resolve_all_new():
    r, _ = _runner(_stub_agent(memory=MagicMock()))
    r.agent.memory.pending_notices.return_value = [
        {"title": "conflict-1", "old_entry": "旧", "new_text": "新"},
    ]
    r.agent.memory.organize.return_value = ["conflict-1 → merged"]
    res = r.dispatch("/memory resolve")
    assert res.confirm is not None and res.confirm.tag == "memory_resolve"
    out = _plain(r.handle_confirm(res.confirm, "keep_new"))
    assert "处理完成" in out


def test_welcome_card():
    r, _ = _runner()
    plain = r.welcome().plain
    assert plain.startswith("◆ EnCoder")
    assert "test-model" in plain