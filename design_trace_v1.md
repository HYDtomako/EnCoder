# Trace 链路观测 — 设计（v1）

> 本文是 `design_trace.md`（讨论稿）的落地设计，一条一条对过之后收口的版本。
> **状态：已实施**（写侧字段 + `llm_call`、读侧 `encoder/trace.py` 与 `/trace`、
> `tests/test_trace.py` 20 例；`name` / `error` 已按 §3.4 删除，旧日志由读侧映射）。
> 文中 `file:line` 是设计当时的代码引用，用来说明"问题在哪"，今天未必还对得上。
>
> 全文只有一把尺子：**能推导的不落盘，落盘的必须是事后无法重建的。**

---

## 1. Trace 是什么

不是新功能，是**同一份日志的第二种看法**：

```
.CHECKPOINT/<session>/events.jsonl ──┬── checkpoint 视图：状态（从哪儿能接着跑）  ← 已实现
                                     └── trace 视图：span（这次任务发生了什么）  ← 本文
```

checkpoint 是给程序看的（少而准的恢复点），trace 是给人看的（按时间排开的一串跨度）。
两者共用一条流、互不干扰：**写日志的那一层不动，trace 只在读的时候把日志组装成一棵树。**

`design_checkpoint.md` §7 承诺过"事件字段按 trace 语义设计，下一步不需要重新布点"——
这份设计就是去兑现它，同时也补上当初没铺到位的那几处。

---

## 2. 现在的问题（6 条）

1. **模型那一步不在日志里。** 它每次调工具前都说过一句话（"先看一下实现，再改校验"），
   那句话在内存里，没人存。`agent.py` 里 12 个 `record()` 调用点
   （`133/187/203/245/284/326/337/354/359/365/439/483`），**没有一个是模型调用**
   → "为什么做这个动作"答不出来。
2. **看不出花了多久。** `ts` 只到秒（`_now()`，`checkpoint.py:184`），本地工具跑不到一秒；
   `tool_start` 只为长调用发（`checkpoint.py:328`）→ 时长算不出。
3. **前后两条配不上对。** `tc.id` 就在手里却从不落盘（`agent.py:326`、`agent.py:435`）
   → 一轮里并行调三个工具，分不清哪条对应哪次调用。
4. **`workspace` 是空字段。** 定义了、docstring 还写着"for the trace"（`checkpoint.py:276-280`），
   但**从来没写过一次**（`checkpoint.py:293`）。
5. **失败只有原文，没有判断。** `classify_error` 的 7 类原因只在内存 digest 里算
   （`checkpoint.py:830`）；而 `Event.error` 字段**只读不写**（读它的是 `checkpoint.py:833/842`，
   那个 `or` 左边永远是空）。
6. **一次会话一条扁平流。** 没有"这次任务"的边界，几件事混在一个文件里；
   队友是黑盒——内部一条事件都没有（`agent.py:113-117` 故意不给它建 checkpoint）。

---

## 3. 定下来的字段

### 3.1 通用字段（每种事件都有）

| 字段 | 装什么 | 处理 |
| --- | --- | --- |
| `seq` / `ts` | 顺序 / 写入时刻 | 保留。**顺序以 `seq` 为准**，`ts` 是给人看的（秒级、本地时间） |
| `type` | 事件类型（开放集） | 保留 |
| `actor` | **谁执行的** | **修**（见 3.4） |
| `status` | `ok` / `error` / `pending` | 保留 |
| `data` | 上下文：**索引和来源，不是副本** | 补一个键（见 3.6） |
| `files` | 这一步声称要动的文件（指针） | 保留 |
| ~~`name`~~ | 万能字段，什么都装 | **删**（见 3.4） |

### 3.2 工具 span（`tool_start` / `tool_done`）

| 字段 | 装什么 | 例子 |
| --- | --- | --- |
| `tool` | 调用了哪个工具 | `edit_file` |
| `input` | 输入的参数，**JSON 文本**（可解析） | `{"file_path": "…", "old_string": "…"}` |
| `output` | 工具返回的文本（成功 800 字 / 失败 8000 字） | `Error: old_string not found` |
| `call_id` | `tc.id`——**唯一的配对键** | `call_ab12` |
| `duration_ms` | 只有长调用记（见 §4.1） | `9200` |
| `change` | 这一步改了什么（见 3.7） | `[{path, kind, added, removed, patch}]` |
| `status` | `ok` / `error` / `pending`（等人不是失败） | `error` |

配套两条：

- **内容型参数要截断**：`write_file.content`、`edit_file.old_string` / `new_string` 只留
  "多少字节 + 开头 200 字"。现在是不截断的（`agent.py:330`），等于每次写文件都把正文抄进日志
  ——这是**当前就存在的 bug**，不是设计取舍。
- `data.args`（dict 版参数）**保留**：读侧要 dict 时不必去解析字符串，那点重复是故意留的。

### 3.3 模型 span（新事件 `llm_call`）

在唯一那处模型调用（`agent.py:192-196`）外面包一层计时：

```json
{"seq":44,"ts":"2026-09-14 10:31:09","type":"llm_call","actor":"lead","status":"ok",
 "output":"先看一下 edit 工具当前实现，再改参数校验…",
 "data":{"model":"deepseek-chat","duration_ms":3120,
         "prompt_tokens":18422,"completion_tokens":210,"context_tokens":18422,
         "calls":[{"id":"call_ab12","name":"read_file"},
                  {"id":"call_cd34","name":"edit_file"}]}}
```

- `output` = 模型这轮原话的开头（截 300 字）——**这就是"为什么做这个动作"的答案**，白捡的。
- `data.calls` 是**因果边的源头**：它的 `id` 和后面 `tool_done.call_id` 对上，就把
  "谁决定的"和"谁执行的"接起来了。
- 失败也要记（`status="error"`，`output` = 异常文本），否则"agent 卡住了"的 trace
  上会缺最关键的一条。

### 3.4 谁执行：`actor`，删掉 `name`

`actor` 的取值集早就写在注释里了：`lead | agent_N | subagent | integrator | system | user`
（`checkpoint.py:285`）——**设计意图在，但一个都没落到位**：队友的事件写的是 `lead`
（`agent.py:365`），subagent 一条事件都没有。

**契约**：`actor` = 谁执行的；"关于什么"用各自明确的字段，不再有一个什么都能装的 `name`。

| 事件 | actor | 关于什么 |
| --- | --- | --- |
| `tool_done` / `tool_start` | `lead` | `tool` |
| `todo_changed` | `lead` | `data.todo_id` |
| `task_changed` | `lead` | `data.task_id` |
| `teammate_changed` | **`agent_1`** | `data.task_id`（"谁"已在 actor 里，不重复） |
| `manual`（模型打点） | `lead` | `data.label` |
| `session_start` | `system` | `data.session` |

旧日志里的 `name` 由读侧映射到这些槽位，不会读不出来。
代价：`Event` 少一个字段，几处 emit 换个键（`agent.py:354/359/365/439`、`manual` / `rewind`），约 10 行。

### 3.5 状态 span（`todo_changed` / `task_changed` / `teammate_changed`）

**已有的不动**：`data.from` → `data.to` 记迁移，`status` 记结果，`data.title` / `data.description`
带上人读的名字（`agent.py:353-362`）。

**要补的是队友**：队友的状态机是 `idle / work / ending`（`team.py:180/203/217/229`），
真正有意义的两跳现在**一条事件都没有**：

- `status = "work"` —— 开始跑（`team.py:217`，在 `agent.chat` 之前）
- `status = "idle"` —— 跑完（`team.py:229`，`finally` 里，提交完自己分支之后）

各记一条 `teammate_changed(action="status")`，`actor` 是队友名：

```json
{"type":"teammate_changed","actor":"agent_1","status":"work",
 "data":{"action":"status","from":"idle","to":"work","task_id":"t3"}}
```

怎么接：`Teammate` 手里现在只有 `mailbox` 和 `tasks`、没有 manager，所以给它加一个
`on_event` 回调（建 `Teammate` 那处传 `self._notify`），两跳各一行——**2 行 + 1 个参数**。

### 3.6 上下文：`data` + 一个 `prelude`

| `data` 里的键 | 装什么 | 状态 |
| --- | --- | --- |
| `raw` | 用户这次的原话 | 已有（`agent.py:187`） |
| **`prelude`** | 这次请求带了哪些注入块 + 每块开头一句（`task_reminder` / `handoff` / `team_summary`） | **补**。它是唯一真正来自 turn 之外、读本 turn 事件流看不出来的上下文。`design_checkpoint.md` §11.1 写了要记，代码没落地 |
| `from` / `to` / `action` / `reason` / `label` / `next` / `branch` / `worktree` … | 各类型自己的补充 | 已有 |

**不装的**：对话原文（那是 `messages` 和快照的活——同一个 turn 里"前面的那些 span"就是它当时的交流上下文，
顺序本身就表达清楚了）、messages 区间指针、上下文水位（`llm_call.prompt_tokens` 就是它）。

### 3.7 环境：`workspace` 的形状

一个统一形状的环境描述，`kind` 是闭集（和 `trigger` / `reason.kind` / `resume.kind` 一个套路：
**可枚举，认不出的落 `unknown`**）：

```json
"workspace": {
  "kind": "worktree",                       // repo | worktree | sandbox
  "root": "D:/EnCoder/.worktrees/agent_1",
  "branch": "agent_1",
  "head": "55c9e58",
  "worktree": ".worktrees/agent_1",         // kind=worktree 才有
  "agent": "agent_1",                       // kind=worktree 才有：谁的工作区
  "sandbox": null                           // 预留，现在没这机制，不假装有
}
```

- `kind` 只答"这个环境长什么样"，**不答"谁在里面"**——subagent 复用父目录，所以它的 `kind`
  就是 `repo`，谁在用由 `actor` 表达；将来 subagent 真有了隔离，它自然变成 `worktree`，字段不用改。
- **只在环境变了时盖章**（manager 缓存一份 env，快照时刷新——`_env_view` 本来就要 shell out 到 git，
  `checkpoint.py:1150`）。每条都盖的代价是每条事件问 3 次 git；变化才记同时还表达出
  "这一步 `cd` 了 / 换了分支"这个事实。
- 读侧向后继承最近一次盖章；**旧日志没有这个字段就继承最近快照的 `env`**，所以老日志照样能读。
- 界：文件层面**只到 `root` 这个粒度**。"改了哪些文件"是 `change` / `files` 的事，不是这里的事。

### 3.8 改了什么：`change`

现状：`edit_file` **已经会算 unified diff** 并塞进返回值（`_unified_diff`，`edit.py:79-92`，
截断 3000 字），而返回值就是 `output`——**但成功调用的 `output` 只留 800 字**（`_OUTPUT_LIMIT`，
`checkpoint.py:101/729`），改动的证据被截掉；`write_file` 更狠，只返回 `Wrote N lines`。

所以把"改了什么"提成独立字段：

```json
"change": [{"path":"encoder/tools/edit.py","kind":"edit",
            "added":3,"removed":1,
            "patch":"@@ -54,6 +54,8 @@\n-    occurrences = content.count(old)\n+…"}]
```

- 列表（一次调用可能改多个文件；现在只会有一个，形状先对）
- `kind` 闭集：`edit` / `write` / `delete`
- `patch` 沿用现成的 3000 / 2500 截断（`edit.py:90-91`），超了只留 `added` / `removed` 统计
- **谁产生**：工具自己（它手里有 before / after），事件只负责转述。`edit_file` 直接复用
  `_unified_diff`；`write_file` 要补一步——覆盖前读一下旧内容再 diff（一次 IO），
  否则它永远只知道写了多少行

**纪律修正**：不是"内容归 git、只留路径"，准确的说法是——**不存全文，存差分**。
差分的体量由**改动**决定，不由**文件大小**决定。

---

## 4. 读时算、不落盘的四样

| 什么 | 怎么算 | 为什么能推 |
| --- | --- | --- |
| `description`（人读的一行） | 优先级：**模型自己写的**（`data.reason` / `data.label`）> 参数推的（`edit_file 路径`）> 类型默认文案 | 全是既有字段的函数 |
| `error.reason`（7 类原因） | `classify_error(tool, output, args)`（`checkpoint.py:224`） | 纯函数，输入全在事件里 → **历史日志自动获得原因** |
| `error.repeat`（同一处第几次） | 按 `_error_key` 扫同类失败（`checkpoint.py:1881`） | 同上 |
| `focus`（这一步在服务哪个 task / todo） | **这个落字段**：manager 记"当前在推进的 task/todo"（谁进 `in_progress` 就是谁，完成/归档就清），**变化时才盖到事件上** | 事件里没有，事后推不出（工具调用不带任务信息） |

`focus` 和 `workspace` 我一度改判成"纯推导、不落字段"，最后**改回字段**：推导只在顺序执行时成立，
而且这两条是你点名要的。代价约 30 行，接受。

`review` 不进字段：`review_teammate(name, feedback)` 的 `target` + `feedback` 本来就在
`data.args` 里（`tools/team.py:104-130`），读时按工具名取出来即可——它在 span 上是 `review`，
在容器上工作项清单里就是一条，和 `.TASK/` 的 task 并列。

---

## 5. 要改的地方

| # | 文件 | 改动 | 量 |
| --- | --- | --- | --- |
| 1 | `encoder/agent.py` | `chat()` 里给模型调用包计时 + 记 `llm_call`；`_exec_tool` 记 `t0`；`_record_tool` 写 `call_id`；`tool_start` 补 `call_id`；`record("user_message", …)` 补 `data.prelude` | ~30 行 |
| 2 | `encoder/checkpoint.py` | `Event`：加 `call_id` / `duration_ms` / `change` / `focus` / `workspace`(填活)，**删 `name` / `error`**；`trigger_for` 显式声明 `llm_call → None`；`record()` 加环境盖章 + 内容型参数截断 | ~30 行 |
| 3 | `encoder/team.py` | `Teammate` 加 `on_event`，两处状态跳转各记一条 | ~4 行 |
| 4 | `encoder/tools/edit.py` | 把 diff 从返回值挪成 `change` 字段（`_unified_diff` 复用） | ~10 行 |
| 5 | `encoder/tools/write.py` | 覆盖前读旧内容，生成 `change` 的 `patch` | ~10 行 |
| 6 | `encoder/trace.py` | **新增**：`read_events` / `build_traces` / `render_trace` | ~200 行 |
| 7 | `encoder/cli.py` + `encoder/tui/commands.py` | `/trace` 命令 + `/help` 一行 | ~30 行 |
| 8 | `tests/test_trace.py` | **新增**，见 §7 | ~150 行 |

`Event` 加字段对旧日志零风险（`from_dict` 忽略未知键，`checkpoint.py:304-307`）。

---

## 6. 读侧：`trace.py` 和 `/trace`

三个纯函数 + 一个渲染器（风格对齐 `should_checkpoint` 的纯函数和 `tui/render.py` 的 rich Text）：

```python
read_events(session_dir) -> list[Event]      # 唯一碰 I/O：合并 archive/*.jsonl + events.jsonl，按 seq 排
build_traces(events) -> list[Trace]          # 纯：切 turn、按 call_id 配对、算 duration/reason/description、锚快照
render_trace(trace, *, full=False) -> Text   # 纯：树形文本
```

`read_events` 容错照抄 `EventLog.read()`（坏行跳过、绝不抛，`checkpoint.py:448-467`），
并且要合并归档分片（`archive()` 把旧行搬进 `archive/events-*.jsonl`，`checkpoint.py:490`）
——**trace 天然跨归档**，这是它和只读尾部的 digest 最大的不同。

渲染大概长这样：

```
● t3  10:31:07 → 10:31:22   用户：把 edit 的 old_string 校验改成…
├─ llm  deepseek-chat  3.1s  in 18.4k / out 210  → read_file, edit_file
│      └ 先看一下 edit 工具当前实现，再改参数校验…
├─ tool read_file  ok     encoder/tools/edit.py      ← call_ab12
├─ tool edit_file  ERR    encoder/tools/edit.py      ← call_cd34   [stale_view]
│      └ Error: old_string not found in file
├─ state todo t2  pending → in_progress
├─ change edit  +3 −1  encoder/tools/edit.py
├─ ● cp cp_0007  trigger=error（可 restore）
└─ llm  deepseek-chat  2.4s  in 19.1k / out 180  → read_file
```

入口：CLI `/trace`（最近一个 turn）、`/trace list`、`/trace <n>`；TUI 挂进命令表
（`tui/commands.py:76`，`/checkpoint` 旁边）。**不做实时跟随**——TUI 已经有实时视图，
`/trace` 的价值是事后回看。

---

## 7. 测试计划

1. 切 turn：两条 `user_message` 之间的事件归对容器；`interrupt` 收尾的 turn 不吞下一条
2. `call_id` 配对：`llm_call.data.calls` ↔ `tool_done.call_id`；**缺 llm 事件的老日志**降级成"未归属执行"而不报错
3. `duration_ms`：假事件构造 120ms，断言 `start = end - duration`
4. 失败原因：`edit_file` 的 not-found 判成 `stale_view`、`bash` 的 not-found 判成 `env`（复用 `test_checkpoint.py` 的现成断言）
5. **`llm_call` 不是 checkpoint 触发点**：喂给 `should_checkpoint` 断言 `Decision(False)`——防止后人"顺手"加进 `HARD_TRIGGERS`
6. 环境只在变化时盖章：连记两条同 cwd 的事件，第二条里没有 `workspace`
7. 渲染：合成事件断言输出含 `t3` / `← call_ab12` / `[stale_view]`（纯函数断言，风格同 `tests/test_tui_render.py`）
8. 容错：`read_events` 遇坏行/半行跳过、不抛

---

## 8. 不做（写下来免得回头再讨论）

- **OTel / OTLP 上报** · **采样** · span 级重放（那是 checkpoint 的活）· 跨进程/跨机器追踪
- **文件全文**（存差分，见 3.8）· **LLM 全文**（归 messages / 快照）
- **绝对时间戳 start / end**：`seq` 管顺序、`ts` 管给人看；`duration_ms` 只给模型调用和长调用记
  （普通工具毫秒级，没信息量）
- **队友 / subagent 的内部 span 接进 lead 的日志**：v1 只到"谁参与了"（`actor` 写对 +
  `teammate_changed` 状态 + spawn/release/integrate 带 branch/worktree）。挖开内部会让日志量乘队友数，
  先看一眼真跑出来的 trace 长什么样再决定
- **sandbox**（现在没这机制，`workspace.kind` 里留了位置）· 数据库 / 可视化 UI

---

## 9. 对账：讨论稿那 12 条字段的去向

| 你写的 | 最后怎么处理 |
| --- | --- |
| `name`：哪个 agent 执行 | **改名到 `actor`**（修正队友事件写错的问题），并**删掉万能字段 `name`** |
| 当前的 task / todo / review | `focus` 字段（`task_id` / `todo_id`）；`review` 读时从 `args` 派生 |
| `workspace`：执行环境 | **填活**，统一形状 `{kind, root, branch, head, worktree, agent}`，只在变化时盖章 |
| `tool_name` / `input` / `output` | 已有；`input` 统一成 JSON 文本；内容型参数**必须截断**（修 bug） |
| `description`：动作描述 | **不落字段**，渲染时按三档优先级算 |
| `context`：附加上下文 | 用已有的 `data`，**补一个 `prelude`**；不装对话原文 |
| `start_time` | **不记**（`seq` 管顺序，秒级时间戳没意义） |
| `end_time` | 就用现有的 `ts` |
| 各种状态的变化 | 已有（`from` / `to`）；**补队友的 work/idle 两跳** |
| 执行后变化的内容 | **`change` 字段**（path / kind / added / removed / patch）——存差分，不存全文 |
| 产生的 error | 原文已有（8000 字）；**原因和重复次数读时算**；删掉只读不写的 `Event.error` |
| 是否涉及 user-approval | 已有（`approval` 事件 + `status="pending"`） |

**净新增字段只有 5 个**：`call_id` · `duration_ms` · `change` · `focus` · `workspace`（填活），
加 1 个新事件 `llm_call`；删 2 个（`name` / `error`）；修 1 个（`actor`）。
