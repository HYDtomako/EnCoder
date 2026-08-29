"""AgentBridge —— 把阻塞式 ``Agent.chat`` 转成一个供 TUI 消费的事件流。

核心 Agent(`encoder/agent.py`)保持零改动,接入面只有两条回调:

- ``on_token(content)``:LLM 逐 chunk 流式正文(``llm.py:182``);
- ``on_tool(name, call)``:每次工具调用(``agent.py:161`` 同步 / ``_exec_tools_parallel``)。

本桥保证同一时刻至多一个回合在工作线程运行,所有事件(流式 token、工具、
回合结束/错误/取消)先进线程安全队列,由 UI 侧定时 drain 到主线程渲染。
取消(``request_cancel``)不杀线程,而是置位标记,工作线程在**下一次回调**时把它
转成 ``KeyboardInterrupt`` 抛给 ``agent.chat`` —— ``agent.chat`` 现有 except 分支会
回填未答复的 tool_calls(``agent.py:177``),之后异常传播到桥,桥**回滚本轮追加的
messages**,保证会话历史一致。

本模块不 import textual,方便 headless 单测。
"""

from __future__ import annotations

import dataclasses
import queue
import threading

from ..agent import Agent


@dataclasses.dataclass
class Job:
    """一次提交(用户请求 / 定时任务 / demo)。single-worker 模型下至多一个在跑。"""

    id: int
    kind: str           # "user" | "cron" | "demo"
    text: str
    extra: object = None  # App 附加上下文(如 cron 的 Task)


class AgentBridge:
    """工作线程串行执行 agent.chat,把内部进展通过事件队列暴露给 UI。"""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._jobs: queue.Queue = queue.Queue()
        self._events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._cancel_flag = False
        self._busy = False
        self._sequence = 0
        self._job: Job | None = None   # 当前在跑的回合
        self._worker = threading.Thread(target=self._run_loop, name="encoder-agent-worker")
        self._worker.daemon = True
        self._stopping = False

    # ------------------------------------------------------------------ UI 侧 API

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        """标记停止:不再接受新提交。在跑回合让线程自然结束(dameon 兜底)。"""
        self._stopping = True

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def current_job(self) -> Job | None:
        return self._job

    def submit(self, text: str, kind: str = "user", extra: object = None) -> Job:
        with self._lock:
            self._sequence += 1
            job = Job(id=self._sequence, kind=kind, text=text, extra=extra)
        self._jobs.put(job)
        return job

    def request_cancel(self) -> None:
        self._cancel_flag = True

    def drain(self, limit: int | None = None) -> list[tuple]:
        """取回已就绪的事件(UI 主线程调用)。事件为 (kind, *payload) 元组。"""
        out: list[tuple] = []
        try:
            while limit is None or len(out) < limit:
                out.append(self._events.get_nowait())
        except queue.Empty:
            pass
        return out

    # ------------------------------------------------------------------ 工作线程

    def _run_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if self._stopping:
                return
            self._run_round(job)

    def _run_round(self, job: Job) -> None:
        with self._lock:
            self._busy = True
        self._cancel_flag = False
        self._job = job
        snapshot = len(self.agent.messages)
        self._events.put(("started", job))

        def on_token(tok: str):
            if self._cancel_flag:
                raise KeyboardInterrupt()
            self._events.put(("tokens", tok))

        def on_tool(name: str, call: dict):
            if self._cancel_flag:
                raise KeyboardInterrupt()
            self._events.put(("tool", name, call))

        try:
            response = self.agent.chat(job.text, on_token=on_token, on_tool=on_tool)
            self._events.put(("ended", response))
        except KeyboardInterrupt:
            # agent.chat 已回填 pending tool calls;整体丢弃本回合,保证历史一致
            del self.agent.messages[snapshot:]
            self._events.put(("cancelled", None))
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能让工作线程死掉
            del self.agent.messages[snapshot:]
            self._events.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            self._job = None
            with self._lock:
                self._busy = False
            self._events.put(("round_done", None))