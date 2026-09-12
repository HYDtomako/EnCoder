"""Tests for AgentBridge's turn lifecycle -- specifically what a cancel leaves behind.

The bridge is the only place that can raise ``KeyboardInterrupt`` into
``agent.chat`` (its two callbacks), so a cancel can be driven deterministically
with a scripted LLM instead of a real Ctrl+C.
"""

from encoder.agent import Agent
from encoder.llm import LLMResponse, ScriptedLLM, ToolCall
from encoder.tui.bridge import AgentBridge, Job


def _tc(name, args, tid):
    return ToolCall(id=tid, name=name, arguments=args)


class _CancelOnTurn(ScriptedLLM):
    """Arm the bridge's cancel flag before turn N, so the *next* callback the
    agent makes turns it into a KeyboardInterrupt.

    This is exactly how a human pressing Ctrl+C mid-turn reaches the bridge:
    ``request_cancel`` sets a flag, and the next ``on_token``/``on_tool`` raises.
    """

    def __init__(self, script, bridge, arm_at):
        super().__init__(script)
        self._bridge = bridge
        self._arm_at = arm_at
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None):
        self._calls += 1
        if self._calls >= self._arm_at:
            self._bridge.request_cancel()
        return super().chat(messages, tools=tools, on_token=on_token)


def _drain_kinds(bridge):
    return [e[0] for e in bridge.drain()]


def test_cancel_keeps_the_turn_so_the_next_message_has_an_antecedent(tmp_path):
    """The turn a cancel interrupts must stay in history.

    Regression: the bridge used to ``del messages[snapshot:]`` here, which also
    deleted the user's own request -- so the *next* message stood alone with no
    antecedent and the model had to guess at it.
    """
    game = tmp_path / "snake.py"
    bridge = AgentBridge(Agent(llm=ScriptedLLM([])))   # replaced below
    # turn 1 writes a file and finishes; turn 2 is cancelled on its tool call
    bridge.agent.llm = _CancelOnTurn([
        LLMResponse(content="", tool_calls=[
            _tc("write_file", {"file_path": str(game), "content": "print('hi')\n"}, "c1")]),
        LLMResponse(content="写好了。", tool_calls=[]),
        LLMResponse(content="", tool_calls=[
            _tc("write_file", {"file_path": str(tmp_path / "b.py"), "content": "x"}, "c2")]),
    ], bridge, arm_at=3)

    bridge._run_round(Job(id=1, kind="user", text="写一个小游戏"))
    bridge._run_round(Job(id=2, kind="user", text="再改一处"))

    assert game.exists(), "the first turn really did produce something"
    assert "cancelled" in _drain_kinds(bridge)

    # the cancelled turn is KEPT: the request that produced the game is still there
    users = [m["content"] for m in bridge.agent.messages if m.get("role") == "user"]
    assert users == ["写一个小游戏", "再改一处"], "a cancel must not erase the user's request"
    # ...and the work it did is still visible to the model
    assert any("snake.py" in str(m.get("content")) for m in bridge.agent.messages)


def test_cancelled_turn_leaves_a_chain_the_api_accepts(tmp_path):
    """Keeping the turn is only safe if the chain is valid: every unanswered
    tool_call must have been backfilled, or the next request gets rejected."""
    bridge = AgentBridge(Agent(llm=ScriptedLLM([])))
    bridge.agent.llm = _CancelOnTurn([
        LLMResponse(content="", tool_calls=[
            _tc("write_file", {"file_path": str(tmp_path / "a.py"), "content": "x"}, "c1")]),
    ], bridge, arm_at=1)

    bridge._run_round(Job(id=1, kind="user", text="写个文件"))
    assert "cancelled" in _drain_kinds(bridge)

    msgs = bridge.agent.messages
    answered = {m["tool_call_id"] for m in msgs if m.get("role") == "tool"}
    for m in msgs:
        for call in m.get("tool_calls") or []:
            assert call["id"] in answered, "an unanswered tool_call would be rejected by the API"
    assert msgs[-1]["role"] == "tool" and msgs[-1]["content"] == "[interrupted]"


def test_error_still_rolls_the_turn_back(tmp_path):
    """An exception is not an interruption: the chain may genuinely be broken
    (no backfill runs), so the turn is still discarded."""
    class _Boom(ScriptedLLM):
        def chat(self, messages, tools=None, on_token=None):
            raise ValueError("upstream exploded")

    bridge = AgentBridge(Agent(llm=_Boom([])))
    bridge.agent.messages.append({"role": "user", "content": "之前的对话"})

    bridge._run_round(Job(id=1, kind="user", text="会炸的请求"))
    assert "error" in _drain_kinds(bridge)
    assert [m["content"] for m in bridge.agent.messages] == ["之前的对话"]


def test_cancel_never_touches_history_from_before_the_turn(tmp_path):
    """The snapshot boundary must not reach further back than the current turn."""
    bridge = AgentBridge(Agent(llm=ScriptedLLM([])))
    bridge.agent.messages.extend([
        {"role": "user", "content": "第一回合"},
        {"role": "assistant", "content": "第一回合的回复"},
    ])
    bridge.agent.llm = _CancelOnTurn([
        LLMResponse(content="", tool_calls=[
            _tc("write_file", {"file_path": str(tmp_path / "a.py"), "content": "x"}, "c1")]),
    ], bridge, arm_at=1)

    bridge._run_round(Job(id=1, kind="user", text="第二回合"))
    assert [m["content"] for m in bridge.agent.messages[:2]] == ["第一回合", "第一回合的回复"]
