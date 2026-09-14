"""Trace —— 把同一份 ``events.jsonl`` 换个看法读：不是「状态」而是「过程」。

checkpoint 视角回答「进程被杀之后从哪儿续」；trace 视角回答「这一轮它到底干了什么、
为什么这么干」。**没有第二份存储，也没有第二个写侧**：两个视角是同一串事件的两种读法
（``design_trace_v1.md`` §1）。所以这个模块是纯读侧——除了 ``read_events`` 碰磁盘。

写侧尽量少写，读侧尽量多算：凡是能从事件里推出来的（一句人读的描述、失败的原因、
这是同一处第几次失败、todo 的上一个状态）都在这里算，不占日志的空间
（同 ``checkpoint.py`` 里 ``classify_error`` 的用法）。

不 import ``encoder.tui``：经典 REPL 也要用 ``/trace``，而 ``tui/__init__`` 会拖进
textual。样式用 rich 的标准色名，两边都能看。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from .checkpoint import Event, classify_error

#: 渲染时的截断长度：一行描述 / 子行里的输出 / 完整模式下的输出
_DESC = 100
_OUT = 200
_FULL_OUT = 1200


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    """一个事件，加上读者需要的、日志里没写的那些话。

    ``workspace`` / ``focus`` 是**继承来的**：写侧只在变化时盖章，这里往回填，
    于是每一步都能回答「在哪儿跑的、为哪个任务跑的」。
    """

    event: Event
    kind: str = ""                 # llm | tool | state | env | checkpoint | ...
    description: str = ""          # 人读的一行
    error: dict | None = None      # 失败的原因（7 类闭集，读时算）
    repeat: int = 0                # 同一处第几次失败（>1 才有意义）
    workspace: dict = field(default_factory=dict)
    focus: dict = field(default_factory=dict)


@dataclass
class Trace:
    """一个 turn：从一条 ``user_message`` 到下一条（或日志末尾）。"""

    index: int = 0
    request: str = ""                       # 用户原话
    prelude: list[dict] = field(default_factory=list)   # 这次请求带了哪些注入块
    started_at: str = ""
    ended_at: str = ""
    steps: list[Step] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        """这一步在服务哪个 task —— 取本 turn 里最后一次盖章的 focus。"""
        for step in reversed(self.steps):
            if step.focus.get("task_id"):
                return str(step.focus["task_id"])
        return ""

    @property
    def errors(self) -> int:
        return sum(1 for s in self.steps
                   if s.kind != "checkpoint" and s.event.status == "error")

    @property
    def checkpoints(self) -> list[str]:
        return [str(s.event.data.get("cp_id", "")) for s in self.steps
                if s.kind == "checkpoint"]


# --------------------------------------------------------------------------- #
# 读：唯一碰 I/O 的地方
# --------------------------------------------------------------------------- #

def read_events(session_dir: Path | str) -> list[Event]:
    """读一个 session 的全部事件，按 ``seq`` 排好。坏行跳过，绝不抛。

    要合并 ``archive/events-*.jsonl``：compaction 会把旧行搬进归档分片
    （``checkpoint.py`` 的 ``EventLog.archive``），而 trace **天然跨归档**——
    这正是它和只读日志尾部的 digest 最大的不同。
    """
    d = Path(session_dir)
    files = sorted((d / "archive").glob("events-*.jsonl")) if (d / "archive").exists() else []
    files.append(d / "events.jsonl")        # 尾部放最后，但不影响下面的排序
    events: list[Event] = []
    for path in files:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue                    # 半行（进程被杀的现场）跳过
            if isinstance(obj, dict):
                events.append(Event.from_dict(obj))
    events.sort(key=lambda e: e.seq)        # seq 是权威顺序，ts 只管给人看
    return events


def read_checkpoints(session_dir: Path | str) -> list[dict]:
    """快照的元数据（``index.jsonl``）：trace 用它把断点锚回事件流。

    坏了就当没有：trace 是只读视图，一份坏索引不该让整条时间线看不了。
    """
    path = Path(session_dir) / "index.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            out.append(obj)
    return out


# --------------------------------------------------------------------------- #
# 算：切 turn、配对、算原因与描述（纯函数）
# --------------------------------------------------------------------------- #

def build_traces(events: list[Event], checkpoints: list[dict] | None = None) -> list[Trace]:
    """把事件流切成一个个 turn，并把每一步该说的话算出来。

    ``checkpoints`` 可选：给了就把断点插到它锚定的那个事件位置（``meta.seq``，
    ``checkpoint.py:1097``），于是「为什么在这里存了一个断点」在时间线上看得见。
    """
    anchored: dict[int, list[dict]] = {}
    for meta in checkpoints or []:
        anchored.setdefault(int(meta.get("seq", 0) or 0), []).append(meta)

    traces: list[Trace] = []
    todos: dict[str, str] = {}       # todo_id -> 上一个状态（事件里没记 from）
    current: Trace | None = None
    workspace: dict = {}
    focus: dict = {}
    errors: dict[tuple, int] = {}    # 同类失败计数，用于「第几次」

    for event in sorted(events, key=lambda e: e.seq):
        # 写侧只在变化时盖章，这里往回继承（focus 里的空串表示「清空了」）
        if event.workspace:
            workspace = dict(event.workspace)
        if event.focus:
            focus = {k: v for k, v in event.focus.items() if v}

        if current is None:
            current = Trace(index=len(traces) + 1, started_at=event.ts)
            traces.append(current)

        if event.type == "user_message":
            # 请求是容器的**头**，不是树里的一步：它开一个新 turn，只是不重复
            # 一遍——头一行已经写着「用户：…」了。会话开始之类在这之前的步骤
            # 留在同一个容器里（那时还没有请求可分）。
            if current.request:
                current = Trace(index=len(traces) + 1, started_at=event.ts)
                traces.append(current)
            current.request = str((event.data or {}).get("raw") or event.input or "")
            current.prelude = list((event.data or {}).get("prelude") or [])
            current.started_at = event.ts
            current.ended_at = event.ts
            continue

        step = Step(event=event, kind=_kind(event), workspace=dict(workspace),
                    focus=dict(focus))
        step.description = _describe(event, todos)
        if event.status == "error":
            args = (event.data or {}).get("args") or {}
            step.error = classify_error(event.tool,
                                        event.output or "", args)
            key = _repeat_key(event, step.error)
            errors[key] = errors.get(key, 0) + 1
            step.repeat = errors[key]
        current.steps.append(step)
        current.ended_at = event.ts

    _insert_checkpoints(traces, anchored)
    return traces


def _kind(event: Event) -> str:
    """这一步属于哪一类（渲染和统计都用它）。"""
    if event.type == "llm_call":
        return "llm"
    if event.type in ("tool_done", "tool_start"):
        return "tool"
    if event.type in ("todo_changed", "task_changed", "teammate_changed"):
        return "state"
    if event.type == "user_message":
        return "request"
    if event.type == "approval":
        return "approval"
    if event.type == "note" and (event.data or {}).get("compact") == "events":
        return "archive"
    return event.type or "note"


def _describe(event: Event, todos: dict[str, str]) -> str:
    """一句话说这一步在干什么。

    优先级照 §4：**模型自己写的**（``data.reason`` / ``data.label``）> 参数推的
    （``edit_file encoder/tools/edit.py``）> 类型默认文案。
    """
    data = event.data or {}
    if event.type == "llm_call":
        calls = [str(c.get("name", "")) for c in (data.get("calls") or []) if c.get("name")]
        return "→ " + ", ".join(calls) if calls else "（没有工具调用，直接答话）"
    if event.type in ("tool_done", "tool_start"):
        target = _target_of(data.get("args") or {})
        if data.get("reason"):
            # 模型自己写的理由最值钱，排在参数推出来的前面（§4）
            return f"{target} —— {data['reason']}" if target else str(data["reason"])
        if event.type == "tool_start":
            return f"{target}（长调用，先记一笔）".strip()
        return target
    if event.type == "todo_changed":
        old, new = todos.get(str(data.get("todo_id", ""))), event.status
        todos[str(data.get("todo_id", ""))] = event.status or ""
        move = f"{old or '?'} → {new}" if new else str(data.get("action", ""))
        return f"todo {data.get('todo_id', '')}  {move}  {data.get('title', '')}".strip()
    if event.type == "task_changed":
        move = f"{data.get('from') or '?'} → {data.get('to') or event.status}"
        return f"task {data.get('task_id', '')}  {move}  {data.get('description', '')}".strip()
    if event.type == "teammate_changed":
        action = data.get("action", "")
        if action == "status":
            return (f"teammate {event.actor}  {data.get('from') or '?'} → "
                    f"{data.get('to') or event.status}  {data.get('task_id', '')}").strip()
        if action == "spawn":
            return (f"teammate {event.actor}  派出  {data.get('task_id', '')}  "
                    f"worktree={data.get('worktree') or '-'}").strip()
        if action == "release":
            return f"teammate {event.actor}  释放（分支留着：{data.get('branch') or '-'}）"
        return f"teammate {event.actor}  {action}  {data.get('task_id', '')}".strip()
    if event.type == "approval":
        command = data.get("command") or event.input or ""
        reason = data.get("reason") or ""
        return f"等人批准 {event.tool}：{command}" + (f"（{reason}）" if reason else "")
    if event.type == "user_message":
        return str(data.get("raw") or event.input or "")
    if event.type == "turn_end":
        return "回合结束"
    if event.type == "interrupt":
        return "中断（有调用没答完）"
    if event.type == "compress":
        return f"上下文压缩（{data.get('layer') or event.status}）"
    if event.type == "manual":
        label = data.get("label") or event.input or "打点"
        return f"模型打点：{label}" + (f"（{data['reason']}）" if data.get("reason") else "")
    if event.type == "rewind":
        if data.get("action") == "reset":
            return f"清空会话（{data.get('messages', 0)} 条消息）"
        # 老日志没有 data.cp_id，但一直有 restored_from（v1 之前就在）
        back = data.get("cp_id") or data.get("restored_from") or ""
        return f"回退到 {back}" if back else "回退"
    if event.type == "session_start":
        return "会话开始"
    if event.type == "note":
        return str(data.get("compress_summary", "") or "")[:_DESC] or "记录"
    return event.type


def _target_of(args: dict) -> str:
    """工具作用于什么（和 ``checkpoint._task_of`` 同一套取法）。"""
    for key in ("file_path", "path", "pattern", "name", "command"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


def _repeat_key(event: Event, reason: dict | None) -> tuple:
    """同一处失败的判据：工具 + 目标 + 原因类别。"""
    args = (event.data or {}).get("args") or {}
    return (event.tool, _target_of(args),
            str((reason or {}).get("kind", "")))


def _insert_checkpoints(traces: list[Trace], anchored: dict[int, list[dict]]) -> None:
    """把快照插到它锚定的位置（在产生它的那个事件之后）。

    ``meta.seq`` 是「写这份快照时日志到哪儿了」，所以那一步就是触发它的那一步。
    """
    if not anchored:
        return
    for trace in traces:
        steps: list[Step] = []
        for step in trace.steps:
            steps.append(step)
            for meta in anchored.get(step.event.seq, []):
                event = Event(seq=step.event.seq, ts=meta.get("created_at", ""),
                              type="checkpoint", actor="system",
                              data={"cp_id": str(meta.get("id", "")),
                                    "trigger": meta.get("trigger", ""),
                                    "label": meta.get("label", ""),
                                    "parent_id": meta.get("parent_id", "")})
                steps.append(Step(event=event, kind="checkpoint",
                                  description=f"{meta.get('label') or meta.get('trigger', '')}",
                                  workspace=dict(step.workspace), focus=dict(step.focus)))
        trace.steps = steps


# --------------------------------------------------------------------------- #
# 渲染：纯函数，输出 rich Text
# --------------------------------------------------------------------------- #

def render_trace(trace: Trace, *, full: bool = False) -> Text:
    """一条 turn 的树形视图。

    ``full=True`` 才把工具输出和补丁铺开：默认只留一行一句，因为 trace 的价值
    是**看清结构**，细节该看的时候再看。
    """
    out = Text()
    head = Text()
    head.append("● ", style="bold green")
    head.append(f"#{trace.index}", style="bold")
    if trace.task_id:
        head.append(f"  {trace.task_id}", style="cyan")
    head.append(f"  {trace.started_at}")
    if trace.ended_at and trace.ended_at != trace.started_at:
        head.append(f" → {trace.ended_at}")
    head.append(f"   {len(trace.steps)} 步", style="dim")
    if trace.errors:
        head.append(f"   ERR×{trace.errors}", style="red")
    out.append_text(head)
    out.append("\n")
    if trace.request:
        out.append("  用户：", style="dim")
        out.append(_clip(trace.request.replace("\n", " "), _DESC) + "\n")
    for block in trace.prelude:
        out.append(f"  注入 {block.get('kind', '?')}：", style="dim")
        out.append(_clip(str(block.get("head", "")), _DESC) + "\n", style="dim")

    env_seen: tuple | None = None
    for i, step in enumerate(trace.steps):
        last = i == len(trace.steps) - 1
        env_key = _env_key(step.workspace)
        if step.kind != "checkpoint" and env_key and env_key != env_seen:
            env_seen = env_key
            out.append_text(_tree_line(False))
            out.append(f" env   {_env_text(step.workspace)}\n", style="magenta")
        _render_step(out, step, last, full=full)
    return out


def render_index(traces: list[Trace], *, limit: int = 20) -> Text:
    """``/trace list``：一眼看完整场会话的结构。"""
    out = Text()
    if not traces:
        out.append("这段会话还没有 trace（没有用户消息，或者日志是空的）。", style="dim")
        return out
    out.append(f"◆ trace（{len(traces)} 个 turn，最近 {min(limit, len(traces))} 个）\n",
               style="bold")
    for trace in traces[-limit:]:
        out.append(f" {trace.index:>3}  ")
        out.append(f"{trace.started_at}  {len(trace.steps):>3} 步", style="dim")
        if trace.task_id:
            out.append(f"  {trace.task_id}", style="cyan")
        if trace.errors:
            out.append(f"  ERR×{trace.errors}", style="red")
        if trace.checkpoints:
            out.append(f"  ●{len(trace.checkpoints)}", style="green")
        out.append(f"  {_clip(trace.request.replace(chr(10), ' '), 60)}\n")
    return out


def _render_step(out: Text, step: Step, last: bool, *, full: bool) -> None:
    event = step.event
    out.append_text(_tree_line(last))
    label = _label(step.kind)
    out.append(f" {label:<5}", style=_label_style(step.kind))
    if step.kind == "tool":
        out.append(f" {event.tool}")
        out.append(f"  {'ERR' if event.status == 'error' else event.status or 'ok'}",
                   style="red" if event.status == "error" else "green")
        if step.description:
            out.append(f"  {_clip(step.description, _DESC)}")
        if event.call_id:
            out.append(f"  ← {event.call_id}", style="dim")     # 配对键：谁决定的
        if event.duration_ms:
            out.append(f"  {_seconds(event.duration_ms)}", style="dim")
        if step.error:
            out.append(f"  [{step.error.get('kind', 'unknown')}]", style="red")
            if step.repeat > 1:
                out.append(f" 第 {step.repeat} 次", style="red")
    elif step.kind == "llm":
        data = event.data or {}
        out.append(f" {data.get('model', '')}")
        if event.duration_ms:
            out.append(f"  {_seconds(event.duration_ms)}", style="dim")
        tokens = data.get("prompt_tokens")
        if tokens:
            out.append(f"  in {_tokens(tokens)}", style="dim")
            if data.get("completion_tokens"):
                out.append(f" / out {data['completion_tokens']}", style="dim")
        out.append(f"  {_clip(step.description, _DESC)}")
    elif step.kind == "checkpoint":
        out.append(f" {event.data.get('cp_id', '')}", style="bold green")
        out.append(f"  trigger={event.data.get('trigger', '')}", style="dim")
        out.append("（可 restore）", style="dim")
    else:
        out.append(f" {_clip(step.description, _DESC)}")
        if step.error:
            out.append(f"  [{step.error.get('kind', 'unknown')}]", style="red")
        if event.status == "pending":
            out.append("  [pending]", style="yellow")
    out.append("\n")

    for line in _details(step, full=full):
        out.append_text(_tree_line(last, sub=True))
        out.append(f" {line}\n", style="dim" if not full else "")

    for change in step.event.change or []:
        out.append_text(_tree_line(last, sub=True))
        # ASCII "-" on purpose: the U+2212 minus is not in GBK, and a Windows
        # console on the GBK codepage dies on it mid-render (the REPL prints here).
        out.append(f" change {change.get('kind', '')} "
                   f"+{change.get('added', 0)} -{change.get('removed', 0)} ", style="green")
        out.append(f"{change.get('path', '')}\n")
        patch = str(change.get("patch", ""))
        if full and patch:
            for pline in patch.splitlines()[:20]:
                out.append_text(_tree_line(last, sub=True))
                out.append(f"   {pline}\n", style=_diff_style(pline))


def _details(step: Step, *, full: bool) -> list[str]:
    """步骤下面那几行：模型的原话一定给，工具输出按需给。

    失败的输出**默认也给**：一行 ``Error: old_string not found`` 就是「为什么卡住」
    本身。成功的输出不是——它多半是文件内容，日志里已经有 change 了。
    """
    event = step.event
    lines: list[str] = []
    if step.kind == "llm" and event.output:
        lines.append(_clip(event.output.replace("\n", " "), _DESC))
    if step.kind == "tool" and event.output:
        if event.status == "error":
            lines.append(_clip(event.output.replace("\n", " | "), _OUT))
        elif full:
            lines.append(_clip(event.output.replace("\n", " | "), _FULL_OUT))
    if step.kind == "approval" and full and event.output:
        lines.append(_clip(event.output.replace("\n", " | "), _FULL_OUT))
    return lines


def _tree_line(last: bool, *, sub: bool = False) -> Text:
    """树线：最后一步收口用 └─，子行缩进对齐。"""
    line = Text()
    if sub:
        line.append("    └ " if last else "│   └ ", style="dim")
    else:
        line.append("└─ " if last else "├─ ", style="dim")
    return line


def _label(kind: str) -> str:
    return {"llm": "llm", "tool": "tool", "state": "state", "env": "env",
            "checkpoint": "cp", "request": "user", "approval": "wait",
            "archive": "arch", "user_message": "user", "session_start": "boot",
            "turn_end": "end", "compress": "comp", "interrupt": "intr",
            "manual": "mark", "rewind": "rew", "note": "note"}.get(kind, kind[:5])


def _label_style(kind: str) -> str:
    return {"llm": "cyan", "tool": "bold", "state": "yellow", "checkpoint": "bold green",
            "approval": "yellow", "env": "magenta"}.get(kind, "")


def _env_key(workspace: dict) -> tuple | None:
    """环境是否真的变了（同一个 repo 换个 actor 不算变）。"""
    if not workspace:
        return None
    return (workspace.get("kind"), workspace.get("root"), workspace.get("branch"),
            workspace.get("head"))


def _env_text(workspace: dict) -> str:
    kind = workspace.get("kind", "?")
    text = f"{kind} {workspace.get('root', '')}"
    if workspace.get("branch"):
        text += f" @{workspace['branch']}"
    if workspace.get("head"):
        text += f" {workspace['head']}"
    if workspace.get("agent"):
        text += f"  ({workspace['agent']} 的工作区)"
    return text


def _diff_style(line: str) -> str:
    if line.startswith("+"):
        return "green"
    if line.startswith("-"):
        return "red"
    return "dim"


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _clip(text, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"
