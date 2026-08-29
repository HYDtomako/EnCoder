"""离线冒烟测试:以无 `pytest-asyncio` 的方式驱动 EncoderTuiApp 跑完整 demo。

对齐计划里的离线验证条目 ``encoder --tui --demo``(无需 API key):
用 ``ScriptedLLM`` + 真实 Agent,在 Textual 的 headless 环境中启动 App,
让脚本化 agent 自动播放 write_file → write_file → bash → 结论 四回合,
人工核对点在这里转化为断言:布局可挂载、回复落地、回合结束恢复空闲、
命令面板内可用。Textual 是项目正式依赖,故本测试可以 import textual。
"""

import asyncio
import tempfile
from pathlib import Path

from encoder.agent import Agent
from encoder.config import Config
from encoder.demo import _script
from encoder.llm import ScriptedLLM
from encoder.tui.app import EncoderTuiApp
from encoder.tui.widgets import Conversation, build_sidebar

FINAL = "All three assertions pass"


def _boot() -> EncoderTuiApp:
    workdir = Path(tempfile.mkdtemp(prefix="encoder-tui-demo-"))
    agent = Agent(llm=ScriptedLLM(_script(workdir)), memory_enabled=False)
    return EncoderTuiApp(agent, Config.from_env(), demo=True)


def _text(app: EncoderTuiApp) -> str:
    return "\n".join(str(t) for t in app.query_one(Conversation).lines)


def test_demo_plays_offline_end_to_end():
    app = _boot()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            conv = app.query_one(Conversation)
            # demo 在首次 refresh 后自动播放;轮询等待四回合走完并恢复空闲
            for _ in range(200):
                await pilot.pause(0.1)
                text = _text(app)
                if not app.bridge.busy and not app._busy and FINAL in text:
                    break
            assert FINAL in _text(app)          # 脚本最后一句回复落地
            assert app._busy is False           # 回合结束:忙状态复位

            # 命令面板在 App 生命周期内可用(/tokens 应追加输出行)
            before = len(conv.lines)
            app._handle_typed("/tokens")
            await pilot.pause(0.05)
            assert len(conv.lines) > before

    asyncio.run(scenario())


def test_sidebar_builds_without_errors():
    """侧栏(build_sidebar)对真实 agent 不抛异常,且体现空闲状态。"""
    app = _boot()
    body = build_sidebar(app.agent, False)
    assert "idle" in body.plain
    assert "scripted-demo" in body.plain  # 模型名进了状态卡