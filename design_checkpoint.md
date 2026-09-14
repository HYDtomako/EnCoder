# Checkpoint 断点恢复 — 设计

> 本文是 `design_ckeckpoint.md`（讨论稿）的落地设计，回答它的 a / b / c 三问。
> 参考：LangGraph Checkpointer、JSONL 会话日志（pi-mono / AgentScope / modular_agent_core）、Temporal 事件溯源。
>
> **状态：§1–§10 已实现**（`encoder/checkpoint.py`、`encoder/tools/checkpoint.py`，及各层接线），
> **§11（v2 修订）也已实现**——它回答 `review01.md` 的三个问题，落到 §11.5 的六个文件，
> 测试见 `tests/test_checkpoint.py` 的 v2 章节（含端到端：恢复后回复「继续」确实把原任务带进请求）。
> **§11.7 也已实施**——它补上 `design_process/04.md` b⑤ 那个真缺口（长耗时调用在**执行前**留 mark），
> 全套 324 passed。
> 仍**未做**：讨论中提过的「廉价时间兜底」（只读会话也能留下恢复点，§11.5 表末"不做"里没有它，
> 但它也没成文——因为**还没拍板**），以及 §11.5 表末「不做」一节列的自动重放。
> 文中所有 `file:line` 都是当前代码的事实引用，用于说明"缺口在哪"。

---

## 1. 定位与边界

**Checkpoint = Agent 的可恢复状态，不是文件快照。**

一件事先说清：**这不是 git checkpoint / snapshot**。不存文件内容、不建 shadow git repo、不做文件回滚。文件归 git 和 `.worktrees/` 管——项目里已有 worktree 隔离机制，再自造一套文件快照等于重复造 git，且风险更高。

第二条原则：**已经落盘的东西不重复存。**

| 已有的持久化 | 落到哪 | checkpoint 的态度 |
| --- | --- | --- |
| 任务 | `.TASK/<slug>.json` | 只存**视图**（谁在跑、谁认领），真源不动 |
| 记忆 | `.MEMORY/` | 完全不碰 |
| 队友信箱 | `.Mailbox/` | 不碰 |
| 队友隔离 | `.worktrees/` | 只记录引用（路径 / 分支名） |
| 会话消息 | `~/.encoder/sessions/*.json` | 自动落盘这一份（原来只有手动 `/save`） |

**明确不做**：文件级快照/回滚 · OS 级 sandbox（另见 `design_sandbox.md`） · 数据库/分布式存储 · checkpoint DAG 分支体系 · trace span 上报（本次只按其字段语义预留）。

---

## 2. 目录结构

一个会话一个目录，全部落在**仓库内**（与 `.TASK/` / `.MEMORY/` / `.Mailbox/` 同一约定）：

```
.CHECKPOINT/<session_id>/
  events.jsonl    ← append-only 事实源，崩溃最多丢最后一行
  cp_0001.json    ← 状态快照（事件流的物化视图）
  index.jsonl     ← 派生缓存，可随时重建（只为 list 快）
  head.json       ← 当前指针；restore 只动它，绝不改历史
  archive/        ← compaction 归档，原件搬走不删
```

**为什么放仓库内而不是 `~/.encoder/`**：checkpoint 里存了 `cwd` / `git HEAD` / `branch` / `worktree`，它天然属于"这个仓库"。放进用户主目录会出现"换个目录恢复就错配"，还得额外做校验。

### 2.1 `events.jsonl` — 事实源（append-only）

**一行一个 JSON 事件，追加写、每条 flush。** 记录"发生了什么变化"，是整份设计的**唯一事实来源**（source of truth）。

- `seq` 单调递增，进程内自增、启动时从文件末尾恢复。
- **崩溃语义**：最多丢最后一行；读到半行/坏行**直接跳过**，不抛异常（沿用 `encoder/session.py:68` 的容错风格）。
- 由**多个线程**写入：Lead 在主线程，teammate 在各自线程（`team.py:381-394` 每个 teammate 一个 Agent 实例）——所以写入必须持锁。

事件样例：

```jsonl
{"seq":41,"ts":"2026-09-12 10:31:07","type":"tool_done","actor":"lead","name":"edit_file","tool":"edit_file","files":["encoder/agent.py"],"status":"ok","output":"...","workspace":{"cwd":"D:/EnCoder","branch":"main"}}
{"seq":42,"ts":"2026-09-12 10:31:09","type":"todo_changed","actor":"lead","data":{"todo_id":"t2","from":"pending","to":"in_progress"}}
{"seq":43,"ts":"2026-09-12 10:31:14","type":"compress","actor":"system","data":{"layer":2,"tokens_before":91234,"tokens_after":41022}}
```

**字段是按 `design_trace.md` 的 span 语义设计的**（谁 / 何时 / 调用了什么 / 输入输出 / 成没成功 / 什么环境）。这是刻意的：下一步做 trace 时，不需要重新布点。见 §7。

### 2.2 `cp_0001.json` — 状态快照（物化视图）

**某一刻的完整可恢复状态**，是 `events.jsonl` 的一次**物化视图**（materialized view）。恢复时只读这一个文件，不需要重放引擎。

固定 4 位序号 + 递增，便于排序与比较。

```json
{
  "meta": {
    "id": "cp_0007", "parent_id": "cp_0006",
    "created_at": "2026-09-12 10:31:14",
    "trigger": "compress", "label": "压缩前的 work_state",
    "seq": 43, "compacted": false, "replaces": []
  },
  "state": {
    "agent":     { "messages": [], "todos": [], "tasks": [], "context": {} },
    "execution": { "pending_tool_call": null, "running": [], "pending_approval": null,
                   "last_error": null,      // ← v2，见 §11.2
                   "resume": null },        // ← v2，见 §11.3
    "env":       { "cwd": "", "git_head": "", "branch": "", "dirty": [], "worktrees": [] }
  }
}
```

> `resume` 与 `last_error` 是 v2 新增的两个字段，回答"恢复后接着做什么"和"上次为什么卡住"。
> 它们是本文 §11 的主题，也是 §1–§10 落地后暴露出来的真正缺口。

三条硬性约定：

1. **不可变**。写下去就不再改。restore 后写的是**新** cp，不是回改旧 cp。
2. **原子写**。`tmp` + `replace()`，沿用 `.TASK/`（`task.py:282-288`）和 `.Mailbox/`（`team.py:101-105`）已有的工程约定。
3. **`parent_id` 形成链**，可用 `/checkpoint list` 展示 lineage。

### 2.3 `index.jsonl` — 派生缓存（可删）

`/checkpoint list` 需要列出一堆 cp 的元信息。读 50 个全量快照（每个几百 KB）太慢，所以把 `meta` 单独追加成一行。

**它不是真源**：任何时候删掉都行，`rebuild_index()` 扫 `cp_*.json` 就能重建；发现不一致（行数对不上、指向不存在的 cp）就自动重建。规则很简单——**权威性为零，只是缓存**。

### 2.4 `head.json` — 当前指针

```json
{"head": "cp_0007", "updated_at": "2026-09-12 10:31:14"}
```

**restore 只改这个指针，绝不修改历史文件。** 这是 LangGraph "time travel 永不 mutate 旧 checkpoint" 原则的落地。

副作用是好的：rewind 天然安全、可反复横跳，`/checkpoint list` 能一直看到完整历史。

### 2.5 `archive/` — 归档区（搬走不删）

compaction 把被合并的 cp 和旧事件日志**移到这里**，而不是删除。

```
archive/
  cp_0003.json
  cp_0004.json
  events-20260912-103000.jsonl
```

代价是磁盘占用，收益是"压错了还能捞回来"——对一个还在演进的功能，后者更重要。真要清理时手动删目录即可。

---

## 3. 核心流水线：event → state → checkpoint

这是 `design_ckeckpoint.md` b 条那句话的落地：

```
发生事件 → ① 永远追加进 events.jsonl
         → ② 过 should_checkpoint() 策略 → 有时才写 cp_XXXX.json
```

**第 ① 步永远执行**（便宜、顺序天然、崩溃安全）；**第 ② 步才是"不要无脑保存"的落点**。事件与快照的关系，就是"日志"和"日志的一次物化"。

保存的内容按三层组织，只存"丢了回不来"的：

### Agent State

| 字段 | 说明 |
| --- | --- |
| `messages` | 全量。原先只有手动 `/save` 才落盘 |
| `todos` | 全量。**原先纯内存**（`task.py:90` 注释写明 not persisted），新请求还会主动 `clear()`（`cli.py:375`） |
| `tasks` | 只存视图 `[{task_id, state, assignee, path}]`，真源仍在 `.TASK/` |
| `context` | **索引，不是 prompt**（见下） |

**`context` 这一项是刻意的设计**：不保存一大段 prompt 文本，只保存重建它所需的索引——

- `summary`：最近一次压缩产生的摘要
- `files`：本会话改过的文件，复用现成的 `_changed_files`（`tools/edit.py:15`）
- `focus`：当前推进的 task / todo id
- `token_estimate`：压缩水位

恢复时由这几个字段**重拼**一段说明（风格对齐 `TaskManager.pending_reminder()`、`TeamManager.render_summary()`），而不是回灌旧 prompt。

> 关于 `plan`：`design_ckeckpoint.md` 提到要存 plan，但本项目的 plan 实际就是 **todos + 根任务依赖树**，再加一层字段就是重复。故不引入。

### Execution State

- `pending_tool_call`：中断时未答复的 `tool_call_id`（`agent.py:250-264` 那个补丁正对应这个断裂）
- `running`：teammate 列表（名字 / status / 分支 / worktree）—— 原先只活在 `Teammate` 对象里（`team.py:174-186`）
- `pending_approval`：等待人工批准的高危命令

### Environment State

- `cwd` / `git_head` / `branch` / `dirty` / `worktrees`
- 恢复时**比对指纹并报告**，但**不自动回滚文件**。

---

## 4. 什么时候存：事件驱动的策略层

对应 `design_ckeckpoint.md` b 条的 8 个触发点，落到本项目的具体位置。分**硬触发**（必存）和**软触发**（节流后存）：

| 讨论稿的触发点 | 本项目落点 | 级别 |
| --- | --- | --- |
| ① 不可逆工具执行前后 | `agent.py:173-178`（单）/ `182-187`（并行）之后 | **软**：仅 write/edit/bash/team 类；`read`/`grep`/`glob`/`list_*` 一律不触发；距上次 ≥N 秒或 ≥M 事件 |
| ② todo / task 状态变化 | `TaskManager.save()`、`TodoList.create/update/clear` 挂 `on_change` | **软**：事件必记，cp 走节流 |
| ③ context 压缩 | 包一层 `_maybe_compress()`，**压缩前**必存 | **硬** |
| ④ agent 暂停 / 人工审批 | 识别 `"⛔ Needs your confirmation"`（`tools/bash.py:133`） | **硬** |
| ⑤ 长耗时 / 高危命令 | 高危命令同 ④（bash confirm 一条路径）；长耗时命令另有**执行前**的 `tool_start` marker → §11.7 | **硬** |
| ⑥ 阶段性任务完成 | task 变 completed / archive 后；另有模型主动打点 | **硬** |
| ⑦ 整个任务完成 | 回合边界（`agent.py:151-162` 无 tool_call 返回文本前） | **硬** |
| — 中断 | `except KeyboardInterrupt`（`agent.py:188-192`），带 `pending_tool_call` | **硬** |
| — **卡住（失败）** | `_record_tool` 的 `status=error`（`agent.py:300`） | **硬**：首次「未解决」的错误；同一错误重复时降级为软 → §11.2 |

两个补充机制：

- **去重**：算 state 指纹（messages 尾部 + todos + task 视图 + env），和上一个 cp 相同就跳过。
- **模型主动打点**：加一个 `checkpoint(label)` 工具，让 Lead 在"阶段性完成 / 准备 review"这类**只有模型自己清楚**的语义节点打点（对应 ⑥⑦ 中代码看不见的那部分）。

③ 值得单独说：**"压缩前必存"直接对着讨论稿那句"重要的是截断时的 work_state"**。当前 `ContextManager` 只在内存里重建 messages（`context.py`），压缩前的工作状态是真丢——这是本设计收益最直接的几处之一。

### 4.1 判定函数：`should_checkpoint(event, stats) -> Decision`

**纯函数**——只吃事件 + 统计量，不碰 I/O，因此可以单测。四层门，逐层短路：

```python
Decision = should_checkpoint(event, stats)
# stats: 上次 cp 的时间/序号、累积事件数、上个 cp 的 state 指纹
```

**第 0 层 · 门禁**（不看内容，直接挡）

- 功能关闭 → skip
- `event.type` 属于 `NEVER` → skip
- **已有快照正在写** → skip（teammate 线程与 Lead 线程会并发写，这一条把两次合并成一次，避免抢锁）

**第 1 层 · 硬触发**（绕过第 2 层的节流）

```
HARD = { interrupt, approval, compress, turn_end, stage_done,
         task_done, rewind, manual,
         teammate_spawn, teammate_release, teammate_integrate }
```

**第 2 层 · 软触发**（先过节气门）

```
SOFT        = { tool_done(仅不可逆工具), todo_changed, task_changed }
IRREVERSIBLE = { write_file, edit_file, bash, spawn/release/review_teammate,
                 integrate_results, create/delete_schedule }

放行条件： events_since_last >= SOFT_MIN_EVENTS(5)   OR   elapsed >= SOFT_MAX_INTERVAL(60s)
```

两个刻意的取舍：

- **只读工具（`read` / `grep` / `glob` / `list_*`）永远不进 SOFT**——它们不产生任何不可回退的变化。Checkpoint 是为"不可逆"存在的，不是为"发生了什么"存在的。
- **teammate 的内部工作状态不触发快照**——快照的是 Lead 的状态，teammate 中间干了什么并不改变它；只有 spawn / release / integrate 改变了 env，所以那三个是硬触发。

**第 3 层 · 指纹去重**（硬触发也要过）

`指纹 = hash(messages 尾部 + todos + task 视图 + env)`，与上个 cp 相同 → skip，`reason=no_change`。

**唯一例外是 `manual`**（模型主动打点 / 用户显式命令）：状态没变也记。它表达的是"这是个里程碑"，label 本身有价值，去重会把语义丢掉。

**返回值** `Decision(should, trigger, reason)`

`reason` 落进 cp 的 `meta`，使 `/checkpoint show <id>` 能回答"**为什么这里有个断点**"，而不是让人对着时间戳猜。这对调试策略本身很重要。

**`event.type` ≠ `trigger`，两个词汇表故意分开**

| | 性质 | 服务于 |
| --- | --- | --- |
| `event.type` | **开放集**，可随时新增 | trace 的 span |
| `trigger` | **闭集**，必须可枚举 | compaction 的里程碑过滤（§6） |

两者之间需要一次**显式映射**。新增事件类型时若没声明它映射到哪个 `trigger`，compaction 会静默漏掉它——里程碑就守不住了。

---

## 5. `parent_id` 与 rewind

**做 `parent_id` 链；rewind 只回退 Agent 状态，不碰文件。**

`/checkpoint restore cp_0007` 的完整动作：

1. 还原 messages / todos / context 游标；
2. **修复消息链**——快照可能正好落在"assistant 有 tool_calls、tool 没答复"的位置（正是中断场景），复用 `_answer_pending_tool_calls` 的语义回填 `[interrupted]`，否则 OpenAI 兼容 API 会直接拒绝这次请求；
3. **重拼 context 块**（见 §3），不灌旧 prompt；
4. **打印环境差异报告**：HEAD 变了没、dirty 文件变了没；
5. 提示用户自行处理文件（`git stash` / `git checkout`）；
6. 写**新** cp，`trigger=rewind`，`parent_id` 指向被回退的那个。

第 4、5 步是有意为之：`design_ckeckpoint.md` 问"对某个输出不满意，能不能回到上一 checkpoint 再跑一遍"——能，但**回退的是 Agent 的脑子，不是磁盘**。文件状态交给 git，避免出现"Agent 以为文件是 A、实际是 B"的静默错配。

---

## 6. Compaction（`cp_1..cp_100 → cp_11`）

对应讨论稿 c 条。

**保留**：第一个基线 + 最近 N 个（默认 10）+ 所有里程碑（`trigger ∈ {turn_end, compress, approval, interrupt, stage_done, task_done}`）。

**合并**：其余按时间连续段合成**一个** cp，messages 用 LLM 摘要成一条——**复用现成的 `ContextManager._get_summary()`**（`context.py:164`），无 LLM 时回退 `_extract_key_info`（`context.py:202`）。新 cp 的 `parent_id` 接**最老被合并者的 parent**，保证链不断。

**事件日志同步压缩**：把最老保留 cp 之前的事件折叠成一个 `compaction` 标记事件（pi-mono / modular_agent_core 的做法——记标记，而非假装历史不存在）；原始 `events.jsonl` 归档。

**归档不删除**：被合并的 cp 和旧日志移入 `archive/`。

**触发**：cp 数超过上限（默认 50）自动执行，或 `/checkpoint compact` 手动。

---

## 7. 与 trace 的协同（顺带回答 `design_trace.md`）

事件字段（`actor` / `ts` / `tool` / `input` / `output` / `status` / `files` / `workspace`）是**按 trace 的 span 语义设计的**。

于是同一份事件流有两种视图：

```
events.jsonl ──┬── checkpoint 视图：状态（可恢复）
               └── trace 视图：span（可观测）
```

`design_trace.md` 最后问"哪些字段"、"未来要拿 trace 回答哪些问题"——本次虽然只做 checkpoint，但事件模型已经按那 10 个问题铺好了字段，**下一步做 trace 时不需要重新布点**。

---

## 8. 现状缺口（为什么值得做）

| # | 缺口 | 位置 |
| --- | --- | --- |
| 1 | **todos 完全不落盘**，新请求还主动清空 | `task.py:90-129`、`cli.py:375` |
| 2 | messages 不自动落盘，仅手动 `/save` | `cli.py:324-327` |
| 3 | context 压缩前的 work_state 永久丢失 | `context.py` |
| 4 | 执行中态（未答复 tool_call、待批准命令）无记录 | `agent.py:250-264`、`tools/bash.py:133` |
| 5 | `_changed_files` 是内存集合，崩了就没了 | `tools/edit.py:15` |
| 6 | teammate 运行态（worktree / 分支 / status）只在内存 | `team.py:174-186` |

现有"存档"能力只有 `session.py` 的 save/load（仅 messages + model，`cli.py:113-121` 的 `-r`），以及 TUI 取消时的 `del self.agent.messages[snapshot:]`（`tui/bridge.py:123`，只回滚本回合且不落盘）。

**目前不存在任何 agent 级的 checkpoint / event log / undo。**

---

## 9. 工程约定（沿用现有）

- **原子写**：`tmp` + `replace()`（`task.py:282-288`、`team.py:101-105`）
- **加锁**：teammate 线程与 Lead 线程并发写事件，`CheckpointManager` 持 `threading.Lock`（同 `TaskManager` / `Mailbox`）
- **时间戳**：`_now()` / `_ts()`（`task.py:51-56`）
- **id 风格**：`cp_` + 4 位定宽序号；会话目录沿用 `session.py` 的 `_normalize_session_id`
- **容错读**：坏行跳过，绝不因运行时数据损坏而崩（`session.py:68` 风格）
- **失败可降级**：checkpoint 写入失败不得中断 Agent Loop——与 memory / team 的"失败可降级"原则一致

---

## 10. 落地清单（未实施）

1. 新增 `encoder/checkpoint.py`：`Event` / `EventLog` / `CheckpointManager` / `should_checkpoint` 策略
2. 改 `encoder/agent.py`：建 manager、`_maybe_compress()` 包装、工具执行后记事件、中断与回合结束强制快照、`restore_state()`
3. 改 `encoder/task.py`：`TaskManager.save()` / `TodoList` 加 `on_change` 回调（最小侵入）
4. 改 `encoder/tools/bash.py`：审批哨兵提为模块常量
5. 新增 `encoder/tools/checkpoint.py`：`checkpoint(label)` 工具并注册
6. 改 `encoder/cli.py` + `encoder/tui/commands.py`：`/checkpoint list|show|restore|compact`、`--resume-checkpoint`
7. 改 `encoder/config.py`：`ENCODER_CHECKPOINT_ENABLED` / `_DIR` / `_KEEP` / `_MAX`
8. 改 `encoder/team.py`：`TeamManager` 加 `on_event` 上报
9. 改 `.gitignore`：加 `.CHECKPOINT/`（顺手补漏掉的 `.MEMORY/`）
10. 改 `README.md` / `prompt.py`；新增 `tests/test_checkpoint.py`

---

# 11. v2 修订：让「恢复」变成「续跑」（回答 `review01.md`）

## 11.0 三个 review 点其实是同一件事

现状：`/checkpoint restore` 把 messages/todos 换回去，把交接单 park 在 `_last_handoff`
（`checkpoint.py:481-488`），**等下一条用户输入**才注入（`agent.py:166-169`）。于是恢复的语义是
「agent 失忆了，手里捏着一张便条」，而不是「agent 接着干」。

review01 的三条，是同一个断层在三个切面上的表现：

| review 点 | 缺的东西 | 落点 |
| --- | --- | --- |
| ① 继续型前缀 | 恢复后**要不要/怎么**自动接着做 | §11.1 |
| ② error / last_error | 续跑时**为什么上次停在这** | §11.2 |
| ③ review 继续 | 续跑时**下一步具体做什么** | §11.3 |

所以 v2 引入一个贯穿三者的载体：**Resume 描述符**（`state.execution.resume`，短版进 `meta`）。

## 11.1 继续型前缀：按需改写 + 幂等去重

**触发条件**（已确认「按需改写」的语义）：

| 恢复后的下一条输入 | 行为 |
| --- | --- |
| 空 / 纯继续词（继续、接着、往下、continue…） | 改写成 `继续：<原任务>` |
| 有实际内容的新指令 | **不改写**，新指令优先，待续任务只在交接单里列出 |
| 又是一次 restore | 先剥前缀再重包，**不叠加** |

**幂等规则**（正面回答「多次恢复前缀不能重复，否则语义上有问题」）：

```
strip(x)   去掉开头所有「继续/接着/continue + 分隔符」标记，循环到不动
wrap(x)    f"继续：{strip(x)}"
```

`wrap(wrap(x)) == wrap(x)`。两条保证让前缀不可能堆积：

1. 存进快照的 `resume.text` **永远是 strip 过的裸任务**，包装只发生在注入的那一刻；
2. 即使原料被污染（比如把一次 augment 过的输入又记了一遍），strip 也能吃掉——
   幂等性来自 strip 在 wrap 内部，而不是来自「调用方记得别重复调用」。

**为什么现在做不到**：`agent.py:171-174` 把 **prelude 增强后**的 `user_input` 记进事件，
原始请求文本丢了，而增强文本里本身就含旧交接单。→ 事件改记
`input=<用户原始输入>`、`data.prompt=<实际发出的文本>`、`data.prelude=["task_reminder","handoff"]`。

**抗打转**：`resume.generation` 记录同一 base task 被恢复了几次。`generation >= 3`
或 `last_error.repeat >= 2`（§11.2）时，交接单升级措辞：
「这是同一任务第 N 次恢复，若仍卡在同一处，停下来问用户 / 换策略，**不要重跑同一条命令**」。
这条比前缀本身更重要——反复恢复同一个卡死的断点，比不自动续跑更浪费时间。

## 11.2 last_error：task（在做什么）+ reason（为什么停）+ resolved（是否已解决）

现状四个具体缺陷：

1. `Event.error` 字段声明了但**从来没有人写过**（`checkpoint.py:131`）——死字段；
2. 快照里没有任何错误字段，`render_handoff` 只有「上次中断」两个分支（`checkpoint.py:943-948`），
   从不说「上次为什么失败」；
3. `_record_tool` 只把 `status` 标成 error（`agent.py:300`），错误正文埋在 `output` 里，且
   **`data` 一个字段都没传**（`agent.py:301-305`）；
4. **失败可能根本不触发快照**：`trigger_for` 对只读工具返回 `None`（`checkpoint.py:158`）；
   即便是 bash 这类不可逆工具，错误也只能走 SOFT 节流（5 events / 60s）——
   「卡住的那一刻」和最近的断点最多能差一分钟。

**设计**：三件事缺一不可——**在做什么（task）**、**为什么停（reason）**、**是不是已经修好了（resolved）**。

> v2 初稿在这里只放了一个标量 `last_error`，里面是工具输出首行。那正是「知道做什么、
> 不知道为什么停」：首行是**症状**，不是**原因**。下面是修正后的形状。

### (a) `task` — 出错时正在做的那件事

```json
"task": { "tool": "edit_file", "target": "encoder/agent.py", "command": "",
          "intent": "把 restore 的顺序改成先应用状态、再打 rewind 快照" }
```

`intent` 是这一步**想达成什么**。工具名 + 参数只说明「调了什么」，不说明「为了什么」，
而恢复后要接着做的恰恰是那个意图。`intent` 取不到时退回 `resume.next`（§11.3）。

### (b) `reason` — 归一化后的原因，不是工具输出原文

**原文是症状，原因要判。** `old_string not found` 是症状；原因是
「目标文件在你上次读之后被改过，你的视图过期了」。恢复后的 agent 需要后者才能决定第一步做什么——
而原文它自己重跑一遍就能拿到，不必存。

所以 `reason` 有一个**闭集 `kind`**（和 `trigger` 同一思路：可枚举，恢复后的处置才能直接分支）：

| `kind` | 含义 | 恢复后第一步 |
| --- | --- | --- |
| `stale_view` | 基于过期的文件/分支视图操作 | 重读目标文件、重新 `git status` |
| `not_found` | 路径 / 符号 / 依赖不存在 | 确认路径与拼写 |
| `conflict` | 与其他改动 / worktree 冲突 | 查 worktree 与未归并分支 |
| `permission` | 权限 / 审批被拒 | 问用户 |
| `env` | 缺依赖 / 命令不存在 | 装依赖或换命令 |
| `timeout` | 超时 | 缩小范围重试 |
| `unknown` | 规则判不出来 | 读 `seq` 原文（此时才让 `raw` 进交接单） |

**谁判 kind：规则优先，模型兜底。** edit 的 not-found、bash 的 exit code + stderr、审批=等人，
都能确定性判定；判不出的标 `unknown`，由模型在打点或回合结束时补
（`checkpoint(label, reason=...)`）。**不要每次失败都调 LLM**——贵且慢，而绝大多数失败规则就够。

### (c) 完整形状

```json
"execution": {
  "last_error": {
    "task":   { "tool": "edit_file", "target": "encoder/agent.py",
                "intent": "把 restore 的顺序改成先应用后打快照" },
    "reason": { "kind": "stale_view", "text": "目标文件在本次读取之后被改动过" },
    "raw":    "old_string not found",
    "actor":  "lead",
    "seq": 412, "at": "2026-09-14 10:31:07",
    "repeat": 2, "delta": 37, "resolved": false
  },
  "errors": [ /* 自上个快照以来的错误，未解决的排前面 */ ]
}
```

- `raw` 保留工具原文首行，但**只在 `kind=unknown` 时进交接单**——绝大多数情况下它没有信息增量。
- `errors[]` 是**列表**不是标量：一段时间里可能失败 3 件不同的事，单个 `last_error` 只记得最后一条。
  已解决的错误也留一条短记录（`tool+target+kind+seq+resolved_by`），供"这个坑踩过"查询，但不进交接单正文。

### (d) `resolved` — 错误会过期，防止重复修复

**判定规则两条：**

1. **自动**：同一 `tool + target/command` 之后出现 `status=ok` → 已解决；
2. **显式**：模型声明（有时修复方式是换工具或改文件，不是重跑同一命令）→ 事件带 `data.resolves=[seq]`。

**但判定必须在恢复时重算，不能只信快照。** 快照是**过去某一刻**的物化，它说
`resolved=false` 只代表"那一刻还没修好"——崩溃前最后几秒可能已经修好了，那条成功的
`tool_done` 就在事件日志里。所以：

- 快照里存判定（供 `/checkpoint show` 离线查看）；
- **恢复时以事件日志尾部为准重算**：从 `last_error.seq` 读到日志末尾，找同一 `tool+target/command` 的成功事件。

这让 `seq` 指针有了第二个用途——不只是"取回错误原文"，更是**重算"是否已解决"的起点**。
**这正是"防止重复修复"的落点**：否则恢复后的 agent 会去修一个它自己在崩溃前 3 秒已经修好的问题。

**过期**：`delta = 当前 seq - error.seq`，`delta > N`（如 200）且仍未解决 → 从交接单正文降级为
一行「历史失败（已过期，seq=412）」。一个远古的未解决错误不该每次恢复都念一遍——
**否则"防止重复修复"会变成"反复提醒一件早就不相关的事"**。

### (e) 关键节点保存时，执行期间出现的 error 也要存

- 硬触发（`turn_end` / `compress` / `approval` / `interrupt` / `stage_done`）正是**执行段落的边界**，
  段内的失败必须随段落一起存——否则下一次快照只记得最后一条，段内前两次失败凭空消失。
- **compaction 必须结转**：`_merge_run` 现在只取 run 里**最后一个**快照的 state 并把 messages
  换成摘要（`checkpoint.py:1055-1067`）——早先未解决的 error 会被**合并丢掉**。改成合并各段的
  未解决错误（去重、保留未解决）。
- **teammate 的错误也要带上**：`actor` 记来源（`lead` / `agent_N`）。设计说"teammate 内部工作不触发
  快照"是对的（快照的是 Lead 的状态），但**"队友失败了"Lead 必须知道**——否则就是"队友挂了但我不知道"。

### (f) 触发侧与取回路径

- `tool_done` 带 `status=error` 时写出 `task` / `reason` / `raw`，并**首次失败硬触发**；
  同一错误重复时降级为 soft（避免错误风暴刷屏，但 `repeat` 会累加）。
- 交接单给出 `seq` 指针 + 一行可执行提示：

```
■ 上次为什么卡住
  edit_file 改 encoder/agent.py 失败（第 2 次）：你的文件视图可能过期了
  → 先重读该文件再改；完整输出在 .CHECKPOINT/<session>/events.jsonl 的 seq=412
```

  刻意**不新增取错工具**：`events.jsonl` 是普通文件，agent 现有的读文件能力就能取回完整错误。
- **为了让这个指针真的取回得回**：错误事件的 `output` **不截断**；成功事件的 output 截断到
  `_OUTPUT_LIMIT`。`checkpoint.py:93-94` 的 `_OUTPUT_LIMIT` / `_INPUT_LIMIT` 声明了却**从未被使用**，
  代价是日志把全量工具输出又抄了一份（`_EVENT_TYPES` 同样是死常量，`checkpoint.py:87`）。

## 11.3 resume.next：恢复后知道要 review / 执行什么 command

**最关键的缺口**：`_pending_tool_call_id` 只返回 **tool_call_id**（v1 的 `checkpoint.py:1142-1153`，
v2 已换成返回 `{id, tool, args, why}` 的 `_pending_tool_call`），
工具名和参数都不在快照里。于是恢复后只知道「有个调用没答完」，**不知道要跑什么**。
参数其实一直在事件日志里（`input=str(tc.arguments)`），只是快照没读、交接单也没读。

另一个：`render_handoff` 那段「从事件日志取回的最近动作」本来要显示命令
（`checkpoint.py:929-930` 读 `e.data["command"]`），但 `_record_tool` 从不写 `data`——
**这一列永远是空的**，交接单里最该有信息的一列名存实亡。

**设计**：

```json
"resume": {
  "kind": "task|review|tool|approval|blocked",     // 闭集，恢复时可直接分支
  "text": "<strip 过的原任务>",                     // 继续前缀的原料（§11.1）
  "next": {"tool": "review_teammate", "args": {"name": "rev_2"}},   // 或 {"text": "..."}
  "source_cp": "cp_0007", "generation": 2
}
```

`next` 有两个来源，**先自动、后显式**：

- **自动**：`pending_tool_call` 升级为 `{id, tool, args, why}`；`pending_approval` 升级为
  `{command, tool, reason, seq}`。v1 的 `_pending_approval` 是**正则解析工具输出文本**
  （`checkpoint.py:1156-1166`）——bash 的 confirm 文案一改就失效；v2 由 `bash.py` 在抛确认时
  把 `{tool, command, reason}` 写进线程本地的 `_approval`，`agent._record_tool` 取走后
  **结构化**落一个 `approval` 事件（原先 `tool_done`+`approval` 两张快照合并成一张）。
  扫消息链的版本保留为兜底：从旧日志重建 digest 时没有 approval 事件可读。
- **显式**：`checkpoint(label)` 工具加第二个可选参数 `next`，让模型在「这一步做完了、下一步是 review X」
  这种**只有它自己清楚**的节点写下一条命令。这正是 `design_process/04.md` 的触发点⑥
  「work 完后下一步 review」——代码看不见的那半个触发点，交给模型补。

**review 场景**（review 点 3 的直接回答）：当 `next.tool ∈ {review_teammate, integrate_results}`，
或 in-flight 的工具就是它时 → `kind="review"`，`next` 带上 `name / branch / worktree / target files`，
交接单渲染成：

```
■ 待续 review
  review_teammate(name='rev_2')   ← 分支 teammate/rev_2，改了 3 个文件
  恢复后直接执行这条，不要重新派一个队友
```

即「要 review 什么、跑哪条 command」都从 `resume.next` 里读，而不是让模型对着时间戳猜。

`meta` 里也放一份短的 `resume={kind, text, generation}`：这样 `/checkpoint list` 能直接标出
**可续跑的断点**，不必加载全量 state（`index.jsonl` 本来就是为这个存在的）。

## 11.4 顺带修掉几个真问题（都与恢复语义直接相关）

**① rewind 快照存的是「回退前」的状态，不是「回退后」的。**

`CheckpointManager.restore()` 里先 `record("rewind")` 再返回（`checkpoint.py:851-856`），
而 `Agent.restore_state()` 是**拿到返回值之后**才替换 messages（`agent.py:358-364`）。
于是那张 `trigger=rewind` 的快照收集到的是**旧状态**，而且它成了 head——两个后果：
`/checkpoint list` 显示的 head 与 agent 实际状态对不上；对这个 head 再 restore 一次，
会把刚才的恢复**撤销掉**（回到回退前的状态）。

修法：`restore()` 只记事件、不落快照；`Agent.restore_state()` **先**应用 messages/todos，
**再** `snapshot(trigger="rewind", ...)`——语义回到 §5 第 6 步原本写的样子。

**② 重启后内存摘要全空，重启后的第一张快照缺字段。**

`_files_seen` / `_last_summary` / `_last_compressed_seq` 只在内存（`checkpoint.py:390-393`），
`__init__` 之后从不从日志重建。而「进程被杀」正是这个功能的主场景：重启后第一张快照的
`context.files=[]`、`summary=""`、`compressed_at=0`——交接单说不清「这个会话改过哪些文件」，
`§11.2` 的 seq 指针也无从谈起。

修法：`__init__` 时从日志尾部重建一次 digest（读最后 N 条事件足够），之后增量维护。

**③（附带）`approval` 与它前面的 `tool_done` 会连写两张状态完全相同的快照。**
`approval` 在 `FORCE_TRIGGERS` 里（`checkpoint.py:84`），绕过去重，两张快照只差 `meta`。
合并成一个事件、一个 trigger 即可（顺带解决 §11.3 里那个正则解析）。

**④ 快照里的任务视图可能已经过期，而恢复不会去纠正它。**
`_task_view()` 是按 §3「存视图、真源不动」刻意**冻结**的（`checkpoint.py:567-573`），
而 restore 不碰 `.TASK/`（那是对的：任务归 `.TASK/` 管）。两者叠在一起会出现：
快照说 `t2 in_progress`，磁盘上 `t2` 早已 `completed`。恢复后 agent 拿着过期的视图去
「继续做 t2」，而 prelude 的 `pending_reminder()` 又不提它——自相矛盾且沉默。

这不能靠回滚 `.TASK/` 解决（会和 teammate 的实际产出打架），
应按 §5 第 4 步的同一精神处理：**报告，不行动**。restore 时对照 `.TASK/` 现状，交接单里写
「快照说 t2 进行中，磁盘上是 completed——以磁盘为准」，让 agent 自己重新判断。
和 `env_diff()` 是同一类东西，可以共用一个「drift 报告」的渲染。

## 11.5 影响面（v2，已实施）

| 文件 | 改动 |
| --- | --- |
| `encoder/checkpoint.py` | `resume` / `last_error`（task+reason+resolved）/ `errors[]` 的采集；**恢复时按事件日志重算 resolved**；`reason.kind` 规则分类器（模型兜底 unknown）；错误过期降级；compaction 结转未解决错误；digest 重建；`strip/wrap` 幂等前缀；`_OUTPUT_LIMIT` 真正生效；`approval` 去重；rewind 不再落快照；`.TASK/` drift 报告（复用 `env_diff` 的渲染） |
| `encoder/agent.py` | 记录原始输入；`restore_state` 顺序（先应用、后打 rewind 快照）；失败事件写 `error`；resume 注入时机（按需改写） |
| `encoder/tools/checkpoint.py` | `checkpoint(label, next=...)` 新增可选参数 |
| `encoder/tools/bash.py` | 确认路径结构化上报 command/reason（去掉对文案格式的依赖） |
| `encoder/cli.py` + `encoder/tui/commands.py` | `/checkpoint list` 标可续跑、`show` 显示 `resume`/`last_error`、恢复后提示「直接回车/回复 继续 即可接着做」 |
| `tests/test_checkpoint.py` | 前缀幂等（`wrap∘wrap = wrap`）、未解决错误的判定与消解、resume.next 从 pending tool call 还原、rewind 快照内容 |

§11.7（⑤ 长耗时调用）另动了 `encoder/checkpoint.py`（`LONG_TIMEOUT_SECONDS` / `LONG_COMMANDS` /
`looks_long` / `trigger_for` 新 kind `long` / `_resume_view` 的重跑警告）与 `encoder/agent.py`
（`_exec_tool` 里执行前的 marker），测试 5 条（含"死在调用里"的端到端）。

**不做**：自动重放（恢复后自动跑 `resume.next` 而不经用户）。理由：崩溃恢复的第一条命令没人
审过就打出去，风险高于收益；恢复仍然停在「用户点头」这一步，只是把该说的信息说全。

---

## 11.6 实施后记：落地时才暴露的六件事

设计稿写得再细，也有六处是**写代码时才逼出来**的。记在这里，因为它们都属于「看设计看不出来、
读代码才知道」的那类：

1. **rewind 不能由事件自己触发快照。** §11.4 说"先应用、后打快照"，但 `rewind` 是个硬触发，
   `record("rewind")` 自己就会落一张快照——于是"先应用后快照"被事件抢在了前面。改成 `_log_only`
   （只落事件、不参与策略），快照由 `restore(apply=...)` 在 `apply` 之后显式打。
   顺带一个语义决定：**不传 `apply` 就不打快照**——调用方还没动过 agent，为它写一张恢复点是撒谎
   （`test_restore_without_apply_writes_no_snapshot`）。

2. **重算出来的判定必须被"渲染"，不只是被"算出来"。** 恢复时重算了 `resolved`，但交接单是从
   **快照原文**渲染的——所以它照样念出那条已经被修好的失败。修法是把修正后的 `execution` 块
   拼回一份临时 cp 再渲染。**"信息算对了"和"信息说出来了"是两件事**，这是本次最值钱的一条。

3. **`classify_error` 需要工具名。** `no such file or directory` 是唯一真正歧义的一句：bash 说它
   是"命令不存在"（`env`），文件工具说它是"路径不存在"（`not_found`）——同样的字，恢复后第一步
   完全相反（装依赖 vs 建文件）。工具名本来就在手里，用它消歧即可，不必新增 kind。

4. **"等人批准"不是失败。** 确认路径的结果以 `⛔` 开头，按前缀判状态就会变成 `error`，于是
   一次待批准会被当成"卡住"写进 `last_error`，交接单说错了话。v2 给待批准的工具结果一个
   独立的 `status="pending"`——它既不是成功也不是失败。而 teammate 的 `⛔ Refused` 没有
   pending 标记，仍然是实打实的失败，两者靠这一点区分开。

5. **模型自己写的 `reason` 曾经被丢掉。** `resume.next` 的 `why`（系统推断）渲染了，
   `reason`（模型写的）没有——交接单里最像"人话"的那一句其实是它。

6. **"另有未解决的失败"要去重靠 `seq`，不能靠 `is`。** 快照 JSON 往返之后，`last_error` 和
   `errors[]` 里那条是**两个内容相等的 dict**，`is` 恒为假，于是同一条失败被列了两遍。

---

## 11.7 ⑤ 长耗时调用：marker 写在调用**之前**（已实施）

`design_process/04.md` 的 b⑤（"某些长耗时任务/安装，如 bash command…"）是 §11 落地后剩下的
最后一个真缺口：**别的触发点全都是"事后"的**——`tool_done` 要等命令回来，`error` 要等它失败，
`turn_end` 要等这一轮聊完。一条 `pip install -e .` 跑三分钟，进程在第二分钟被 SIGKILL / 关掉
终端，日志里就只剩**上一条**记录，没有任何东西说明"当时正在跑什么"。

### (a) 触发点：`tool_start`，由两个信号判定，判定是纯函数

```python
LONG_TIMEOUT_SECONDS = 60          # 模型自己声明的 timeout
LONG_COMMANDS = frozenset({...})   # 天生就慢的命令（install/build/test/clone）
looks_long(tool, args) -> bool     # 纯函数，和 should_checkpoint 同一条纪律
```

两个信号，优先级隐含在"或"里：

1. **模型自己写的 `timeout`**——它在说"这条可能会跑很久"。最好的信号：不花钱、不会腐坏
   （没有需要维护的命令表）、而且**在调用前就存在**。
2. **命令本身的形状**——`pip install` / `npm ci` / `cargo build` / `docker build` / `git clone` /
   `pytest`…。只是**信号 1 的地板**：模型忘了设 timeout 的 `pip install`，恰好是失败代价最大的
   那类调用，不能因为没有 timeout 就没有恢复点。

匹配规则刻意收紧，且带一条容易被忽略的处理：**按 `&& | || | ;` 切开逐段看**
——`cd /tmp && pip install -e .` 是安装，不是 `cd`；只看整行开头会漏掉人真正会敲的形状。
段首的 `sudo` / `time` / `env` 会被剥掉。`echo make` 不算构建（匹配的是命令头，不是子串）。

**只有 bash 参与**。其它工具都是"立刻返回"的，给它们打 marker 就是设计里那句"不要无脑保存"。

### (b) 它是硬触发，也是 milestone

- **硬触发**（绕过节流）：这个触发点的全部价值就是"调用没回来时它还在"，被 5 events/60s 节流
  掉就等于没做。
- **milestone**（compaction 不合掉）：`long` 和 `interrupt` / `approval` 是同一类边界
  ——"我正要开始跑一个大的"。
- **不在 FORCE_TRIGGERS 里**：指纹去重照常生效，连着发两条一模一样的 `pip install` 不会写两张
  内容相同的快照。

### (c) 写在哪个位置：`_exec_tool`，而不是两个调用点

`agent.py` 的单次与并行两条路径都汇到 `_exec_tool`，marker 就发在那里，**且参数校验之后**：
参数就不合法的调用根本没开始跑，不该有 marker。

```python
if self.checkpoints is not None:
    from .checkpoint import looks_long
    args = dict(tc.arguments or {})
    if looks_long(tc.name, args):
        self._record("tool_start", actor="lead", name=tc.name, tool=tc.name,
                     input=str(tc.arguments), data={"long": True, "args": args})
try:
    return tool.execute(**tc.arguments)      # 这一步之后就没有 tool_done 了
```

`trigger_for` 仍然自己去读 `data["long"]`，而不是假定"只有长调用才会发这个事件"——
两个词汇表之间的映射是策略层，策略层就该是能单独测的。

### (d) marker 的收益是**信息**，不是回滚

必须说清楚它**不做什么**：它不回滚任何东西。`.TASK/`、工作树、已经装好的半截依赖，
这些东西的事实来源从来不是 checkpoint（§1 的边界）。它买到的只有一样：
**进程死在长调用里时，"当时在跑什么"这句话存在**。

所以交接单里给它配了一句专门的话（`_resume_view`）：如果**没有答复的那次调用**正是长耗时调用，
最可能的故事不是"模型忘了调"，而是"进程死在它执行到一半"。这时候**盲目重跑是唯一可能让事情
变糟的动作**——于是交接单写「先确认它跑到哪里、有没有留下副作用，再决定是接着跑还是重跑」，
而**不替用户做决定**（与 §11.5「不做自动重放」同一条纪律：只报告，不行动）。

### (e) 落地时才看出来的两点

1. **"跑完才算"其实原来是对的，缺的是"开始跑"这个点。** v1 里长命令的恢复点落在它**之前**的
   某个软触发上——落点是对的，但那是"运气对"，日志里并没有为它留下任何判断依据。⑤ 不是修正
   落点，是让落点**有据可依**。
2. **它不该顺手把只读调用也标一遍。** 一度想把 `tool_start` 发给所有工具调用（"反正事件便宜"），
   但事件日志"便宜"不等于"免费"：一次 read_file 也要一次 json.dumps + 一行写盘，而它**永远不会
   长到需要 marker**。发事件的这一层做粗筛，策略层做细判——这也是为什么 `looks_long` 只认 bash。

3. **一句话只能有一个 owner。** 交接单的「■ 待续」和「■ 上次中断」**喂的是同一次未答复的调用**
   （`resume.next` 和 `execu.pending_tool_call` 都由它算出），v1 就把这次调用说了两遍；⑤ 给它
   换了一句更准的"为什么"之后，**两遍开始说不一样的话**——同一行调用，一个说"被中断了"，一个说
   "进程死在它执行到一半"。字面重复只是难看，**解释打架是让人不敢相信这张单子**。
   修法是两步：why 的生成收进 `_why_unanswered`（谁都知道调用没答复，只有这里知道它为什么没答复），
   并且「上次中断」在「待续」已经点名那次调用时不再重复；那句"先问用户，不要自己重跑"移到
   `resume.kind == "approval"` 的分支里，**删掉重复块不等于删掉它要说的话**。
