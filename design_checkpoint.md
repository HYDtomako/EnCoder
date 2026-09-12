# Checkpoint 断点恢复 — 设计

> 本文是 `design_ckeckpoint.md`（讨论稿）的落地设计，回答它的 a / b / c 三问。
> 参考：LangGraph Checkpointer、JSONL 会话日志（pi-mono / AgentScope / modular_agent_core）、Temporal 事件溯源。
>
> **状态：设计稿，尚未实现。** 文中所有 `file:line` 都是当前代码的事实引用，用于说明"缺口在哪"。

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
    "execution": { "pending_tool_call": null, "running": [], "pending_approval": null },
    "env":       { "cwd": "", "git_head": "", "branch": "", "dirty": [], "worktrees": [] }
  }
}
```

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
| ⑤ 长耗时 / 高危命令 | 同 ④，一条路径 | **硬** |
| ⑥ 阶段性任务完成 | task 变 completed / archive 后；另有模型主动打点 | **硬** |
| ⑦ 整个任务完成 | 回合边界（`agent.py:151-162` 无 tool_call 返回文本前） | **硬** |
| — 中断 | `except KeyboardInterrupt`（`agent.py:188-192`），带 `pending_tool_call` | **硬** |

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
