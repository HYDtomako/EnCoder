# EnCoder

这份文档只介绍 CoreCoder 在原项目基础上的新增能力和设计。原项目的基础 Agent loop、模型适配、基础文件工具、上下文压缩和会话功能，请参阅 https://github.com/he-yufeng/CoreCoder。

design_process和corecoder-note里有一些设计过程/学习心得

设计思想参考 https://github.com/shareAI-lab/learn-claude-code



## 新增能力概览

当前版本围绕“让 Agent 能持续推进复杂工作”增加了七组能力：

| 能力 | 解决的问题 | 持久化位置 |
| --- | --- | --- |
| Todo | 当前请求如何拆成轻量步骤并跟踪进度 | 内存，仅当前会话 |
| Task | 如何保存可恢复、可依赖、可派发的工作单元 | `.TASK/` |
| Memory v2 | 如何保存、召回、更新和维护跨回合信息 | `.MEMORY/` |
| Agent teammate | 如何让多个常驻 Agent 独立上下文并行协作 | `.Mailbox/`，可选 `.worktrees/` |
| Checkpoint | 进程被杀之后，Agent 从哪儿接着跑 | `.CHECKPOINT/<session_id>/` |
| 定时触发器 | 如何按每日计划自动提交 Agent 请求 | `~/.encoder/tasks.json` |
| 全屏 TUI | 如何在不改核心 Agent 的前提下提供面板化交互 | 无（纯交互层，`--tui` 启动） |

此外新增了 Tavily 联网搜索和网页正文读取工具。所有新增工具都严格继承 `encoder/tools/base.py` 中的 `Tool` 基类，并把可恢复错误作为工具结果返回，不让单个工具异常打断主循环。TUI 交互层只做界面壳，核心 Agent、工具、记忆、任务、团队全部复用，零核心改动。

## Todo 与 Task

二者有意保持不同：

- **Todo** 是 Lead 针对当前用户请求维护的会话级清单，不写入磁盘、不派发给其他 Agent、没有依赖关系。每个新请求开始时清空，状态为 `pending`、`in_progress` 或 `completed`。
- **Task** 是可跨会话恢复的工作单元。每个活跃任务写入 `.TASK/<可读描述>.json`，任务可以通过 `blockedBy` 组成依赖图，并由 `TaskManager` 统一控制状态流转。

Task 的状态流转为：

```text
pending --(依赖全部完成)--> in_progress --(执行完成)--> completed
```

Task 还提供优先级、执行者、结果、失败记录和最多三次派发尝试。根任务及其已完成的依赖分支可以归档为 `.TASK/done/` 中的一个聚合文件。复杂请求中，Lead 可以根据任务规模自行创建和拆分持久化 Task，而不是等待用户输入“启动 Task”。

新增工具包括：

```text
create_todo       update_todo       list_todos
create_task       list_tasks        update_task
dispatch_task     archive_tasks
```

## Memory v2

记忆系统现在采用“一个分类一个 Markdown 文件，一个主题一个标题块”的布局，避免产生大量 `user-xxx.md` 碎片文件：

```text
.MEMORY/
├── index.json             # 从 Markdown 重建的检索索引
├── long.md                # 长期目标与重大决策
├── short.md               # 短期状态与进行中的事情
├── work.md                # 项目和工作事实
├── personal_prefer.md     # 用户偏好
├── general.md             # 其他记忆
├── .conflicts.json        # 等待用户裁决的冲突
└── .snapshots/            # 删除、合并、迁移前的快照
```

### 记忆处理流程

1. **召回**：先尝试复用最近回合的相关记忆，再使用可选的低成本模型做语义匹配，失败时使用关键词匹配。
2. **注入**：命中的主题以软上下文追加到 system prompt，并明确“仅供参考，以当前请求为准”。
3. **提取**：回合结束时识别长期事实、偏好、决策和工作信息。包含“现在 / 本次 / 暂时”等临时语义的内容不会写入记忆。
4. **更新**：新内容与旧主题相关时追加到同一主题；疑似矛盾时写入 `.conflicts.json`，向用户展示新旧内容，由用户选择保留新内容、保留旧内容或合并。
5. **压缩回写**：上下文压缩生成的摘要也会提取其中的持久事实，避免重要决策随历史压缩丢失。
6. **整理**：超过条目上限时摘要整合，重复主题合并；所有破坏性操作先创建快照。

`index.json` 是派生索引，分类 Markdown 文件才是可人工编辑的事实来源。系统启动或读取时会检查 Markdown 变化并重建索引；旧版 `memory.md` 和分类子目录可自动迁移，并在迁移前保存快照。

## 定时触发器

定时触发器允许用户让 Agent 每天在指定时间自动执行一段请求，例如每日整理资讯、检查项目状态或生成摘要。调度任务包含唯一 ID、请求内容、每日触发时间和上次触发日期，并持久化到：

```text
~/.encoder/tasks.json
```

触发时间使用本地时区的 `HH:MM` 格式。后台调度线程每 30 秒检查一次时钟；同一任务每天最多触发一次。触发器只负责检查时间并把到期任务放入队列，真正的模型调用仍在主线程执行，因此不会打断当前正在进行的 Agent 请求。

在 REPL 中，Agent 可以通过 `create_schedule` 创建每日任务，用户可以用以下命令查看和删除：

```text
/crontab                         查看全部定时任务
/crontab delete <task_id>        删除定时任务
```

默认交互模式会在用户输入间隙执行到期任务。如果需要让定时任务在没有交互输入时也持续运行，可以启动后台模式：

```bash
encoder --daemon
```

后台模式会读取已有任务、等待触发并执行；执行结果同时记录到 `~/.encoder/tasks.log`，方便事后查看。退出交互模式或 daemon 时，调度线程会被正常停止。

## Agent teammate 协作

teammate 与一次性 sub-agent 的区别是生命周期和通信方式：

| | sub-agent | teammate |
| --- | --- | --- |
| 生命周期 | 一个任务完成后结束 | 完成任务后回到 `idle`，等待新任务或 review |
| 上下文 | 独立，但随任务结束 | 独立且跨任务保留 |
| 执行方式 | 同步 | 独立线程并行 |
| 通信 | 返回值 | `.Mailbox/<agent>.json` 消息队列 |
| 结束方式 | 自动结束 | 只有 Lead 发出 `end` 后进入 `ending` |

团队模式默认关闭，由用户通过 `/team on` 明确开启。Lead 只能根据任务规模提出建议，不能自行绕过用户决定启动团队。最多同时运行三个 teammate，并且一个 Task 只能由一个 teammate 认领。

Mailbox 支持五类消息：`task`、`review`、`end`、`result` 和 `notice`。消息按 `end`、`review`、`task`、`notice` 的顺序消费，因此 Lead 的 review 会优先于排队的新任务。队友完成后的结果会写入 `.Mailbox/Lead.json`，Lead 下一次请求时读取并清空，避免重复注入。

会改 repo 代码的 teammate 默认在其独立的 Git worktree（分支 `teammate/<name>_…`）里运行，并在每个工作轮结束自动 commit 到自己的分支；只有研究/只读任务才由 Lead 显式传 `worktree=false` 关闭隔离。worktree 建失败时返回**明确提示“该 teammate 未隔离运行”**（不再静默降级回主目录）。队友拥有独立的 context 和工具实例，但不会继承 `agent`、`dispatch_task` 及团队管理工具，防止递归派发和共享可变工具状态污染。

teammate 完成并 `release_teammate` 后，Lead 调用 `integrate_results` 把其分支归并回当前分支：无冲突的分支由 git 直接合并；**只有真冲突时**才启动一个一次性的 Integration Agent（更优模型，`ENCODER_INTEGRATION_MODEL`）——它读懂双方意图、只重写冲突文件、完成归并并跑测试，再把报告交回 Lead 审阅，Lead 验证后才交付。归并完成的 teammate 其 `.worktrees/` 目录与分支会被自动清理。

新增团队工具包括：

```text
spawn_teammate       collect_results       review_teammate
release_teammate     broadcast_notice     integrate_results
```

## Checkpoint（断点恢复）

**这不是 git checkpoint / snapshot。** 这里不存文件内容、不做文件回滚——文件归 git 和 `.worktrees/` 管。Checkpoint 保存的是 **Agent 自己的状态**，让一个被杀掉的进程能接着干：

| 层 | 存什么 |
| --- | --- |
| Agent State | `messages`、todos、任务**视图**（真源仍在 `.TASK/`）、context 索引（`summary` / `files` / `focus` / token 水位） |
| Execution State | 中断时未答复的 `tool_call_id`、运行中的 teammate、等待人工批准的命令 |
| Environment State | `cwd`、`git_head`、分支、dirty 文件、worktree 列表——**只是指纹，不是内容** |

### 目录结构

```text
.CHECKPOINT/<session_id>/
├── events.jsonl    # append-only 事实源，一行一事件，崩溃最多丢最后一行
├── cp_0001.json    # 状态快照（事件流的物化视图）
├── index.jsonl     # 派生缓存，可随时重建（只为 list 快）
├── head.json       # 当前指针；restore 只追加不动历史
└── archive/        # compaction 归档，原件搬走不删
```

`session_id` 沿用 `session.py` 的命名约定。**重启会认领最新的 session 目录**，而不是新建一个空目录——否则"进程被杀"之后 `/checkpoint list` 只会是一片空白，功能看起来是坏的；要看更早某个目录里的断点，用 `/checkpoint show <id>` 按 id 找（会切过去）。顺带地，消息也不再只靠手动 `/save` 才落盘：每个快照里都有全量 `messages`。

### 什么时候存

```text
发生事件 → ① 永远追加进 events.jsonl
         → ② 过 should_checkpoint() 策略 → 有时才写 cp_XXXX.json
```

事件类型是开放集合（同时为将来的 trace 预留字段），trigger 是封闭集合（compaction 靠它筛里程碑）；两者只在 `trigger_for()` 一处映射。

- **硬触发**（必存）：回合结束、上下文压缩前、等待人工批准、被中断、`/reset`、任务完成、模型主动 `checkpoint(label)`。
- **软触发**（节流后存）：不可逆工具执行后（`write_file` / `edit_file` / `bash` / team 类）、todo/task 状态变化。距上次快照 ≥5 个事件或 ≥60 秒才写。`read_file`、`grep` 这类只读工具只记事件，永不触发快照。
- **去重**：state 指纹没变就跳过；`manual` / `interrupt` / `approval` / `task_done` 是刻意的边界，不受去重影响。

### 恢复

`/checkpoint restore <id>` 恢复的是 **Agent 状态**（messages + todos）。文件不动——环境有漂移会**报告**给你，由你决定是否 `git stash`/回滚。恢复后 Agent 下一条消息会收到一份**确定性交接单**（不是 LLM 生成的）：当前焦点、未完成 todo/task、改过的文件（只是指针）、队友、环境漂移，以及"上次中断在等哪条命令的批准"。交接单最后明确要求 Agent **先重新对焦再动手**，不要假设自己已经了解现状。

两个实现细节值得单独说：

- 快照可能正好落在"assistant 有 `tool_calls`、`tool` 没答复"的位置（正是中断场景），恢复时用 `repair_chain()` 回填 `[interrupted]`，否则 OpenAI 兼容 API 会拒。
- 上下文压缩是本项目**唯一不可逆的上下文操作**（`ContextManager` 的 layer 2/3 会 `messages.clear()` 原地重写）。所以压缩**前**强制快照——这才是设计里强调的"截断时的 work_state"，压缩后再用 `mark_compressed()` 记下截断位置，交接单据此从事件日志里取回被摘要吃掉的动作。因此触发压缩必须走 `Agent.maybe_compress()`，**不能直接调 `context.maybe_compress()`**：上下文那一层没有钩子，绕过去就同时丢掉了截断前的快照和摘要写回记忆的动作。`/compact`（CLI 与 TUI）都是这条路径的调用方。

### Compaction

`cp_0001 … cp_0100 → cp_0011`：保留**第一个基线** + **最近 N 个**（默认 10）+ **所有里程碑**，其余按时间连续段合并成一个——被合并段的消息用 LLM 摘要成一条（复用 `ContextManager._get_summary()`，无 LLM 时回退关键词提取）。合并后的快照沿用该段首个 id，这样 `id` 的字典序仍等于时间序；`parent_id` 接回原 parent，链不断。原件与事件日志一并移入 `archive/`，**不删**。超过 `ENCODER_CHECKPOINT_MAX`（默认 50）自动触发，也可 `/checkpoint compact` 手动。

Checkpoint 在 Lead 上是**按需开启、默认只在 CLI/TUI 的 Lead 开启**——`TeamManager` 为每个 teammate 各建一个 `Agent`，它们不会各自开一个 `.CHECKPOINT/`。踩坑排查点：`team.py` 里 teammate 每轮结束的 `git commit` 与这里的 checkpoint 没有任何关系。

## 联网工具

### `web_search`

通过 Tavily REST API 搜索最新的外部信息，返回标题、URL 和摘要。使用前设置：

```bash
TAVILY_API_KEY=tvly-...
```

### `web_fetch`

读取指定的 `http://` 或 `https://` 链接，使用标准库解析 HTML，移除 `script`、`style` 和标签后返回正文，并限制返回长度。联网工具使用 Python 标准库 `urllib`，不会增加硬依赖。

## CLI 新命令

交互式 REPL 新增以下命令：

```text
/memory                         查看记忆索引
/memory show <name>             查看主题详情
/memory forget <name>           删除主题（保留快照）
/memory organize                整理记忆
/memory resolve                 处理记忆冲突
/memory on | off                开关本会话记忆

/task                           查看活跃任务
/task show <task_id>            查看任务详情
/task update <id> <state|priority>
/task archive <root_id>         归档已完成任务树
/task clear                     清除活跃任务

/team                           查看队友状态
/team on | off                  开关 teammate 模式
/team release <name>            结束指定队友
/team integrate                 把已 release 的代码 teammate 归并回当前分支

/checkpoint                     列出全部可恢复点
/checkpoint show <id>           查看某个断点的 agent / execution / env 详情
/checkpoint restore <id>        把 Agent 状态拉回该断点（不动文件）
/checkpoint compact             合并旧断点，原件移入 archive/
```

以上命令在经典 REPL 与全屏 TUI（`--tui`）中均可使用。

非交互启动参数：

```bash
encoder --resume-checkpoint <id>    # 启动即恢复到指定断点（配合 /resume 之外的冷启动）
```

## 全屏 TUI 交互层

在既有 REPL 之外，新增一个基于 [Textual](https://github.com/Textualize/textual) 的**全屏 TUI 交互层**，配色取自 `tui_image/样式.png`（暖琥珀金 × 近黑炭灰）。它只是一个新的交互壳：核心 Agent、模型适配、工具、记忆、任务、团队系统全部复用，**零核心改动**。默认入口仍是经典 REPL，TUI 通过参数显式启用。

```bash
encoder --tui              # 全屏 TUI 界面（不带 --tui 时行为与以前完全一致）
encoder --tui --demo       # 离线演示：无需 API key，脚本化 Agent 自动播放真实 Agent loop
```

界面构成（与样式图一一对应）：

- **顶部标题栏**：左侧 `◆ EnCoder` 金标，右侧模型名与 base 地址；
- **左侧对话主区**：顶部金色欢迎卡，用户消息带金标，工具调用显示为 `⚙ name(...)`，Agent 回复为内联 Markdown（粗体/斜体/行内代码/标题/代码块）；
- **右侧状态侧栏**：顶部金色状态卡（运行状态 / 模型 / token 用量 / 记忆开关），下方 TASKS、MEMORY、TEAM、CRON 分节列表，每回合结束自动刷新；
- **底部状态栏**：快捷键提示与运行状态，繁忙时金色 `● RUNNING…`。

操作方式：

```text
Enter                发送
Ctrl+J / Alt+Enter   换行
Ctrl+C               中断当前回合（空闲时退出）
/help                查看命令
```

设计要点：

- **线程安全接入**：阻塞式 `Agent.chat` 在工作线程串行执行，`on_token`/`on_tool` 回调把事件放进线程安全队列，UI 定时批量渲染，纯 UI 侧新增，不侵入 Agent Loop；
- **取消不杀线程**：`Ctrl+C` 置位取消标记，下一条回调抛出 `KeyboardInterrupt`，复用 `agent.chat` 已有的中断分支回填未答复的工具调用。**本回合保留在历史里**，不回滚——回滚会把你自己那句请求也一起删掉，下一句话就失去了先行词，模型只能凭空猜（实测：中断后说"保存至一个html中"，模型把仓库里的 Markdown 存成了一个 HTML）。中断是"停下来"，不是"这句不算数"。只有真异常才回滚本回合，因为那时消息链可能真的是断的；
- **后台定时任务**：复用现有调度器，TUI 空闲时自动触发到期任务、执行结果写入 `~/.encoder/tasks.log`，与 REPL/daemon 语义一致；
- **退出清理**：退出时停止调度线程并释放 teammate，与 `cli.py` 的退出路径一致。

新增依赖：`textual`（已写入 `requirements.txt` 与 `pyproject.toml`）。

## 配置项

新增配置均可通过环境变量设置，也可以写入项目根目录的 `.env`：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `ENCODER_MEMORY_ENABLED` | `1` | 启用记忆系统；设为 `0` 关闭 |
| `ENCODER_MEMORY_LLM` | 主模型 | 记忆语义判断使用的低成本模型 |
| `ENCODER_TEAM_ENABLED` | `0` | 是否启用 teammate 模式 |
| `ENCODER_TEAM_MAX` | `3` | 最大并行 teammate 数量 |
| `ENCODER_TEAM_WORKTREES` | `1` | 是否默认用 Git worktree 隔离改代码的 teammate |
| `ENCODER_TEAM_MODEL` | 主模型 | teammate 使用的模型 |
| `ENCODER_TEAM_API_KEY` | 主 API key | teammate 专用 API key |
| `ENCODER_TEAM_BASE_URL` | 主 base URL | teammate 专用 API 地址 |
| `ENCODER_INTEGRATION_MODEL` | 主模型 | 归并冲突时 Integration Agent 用的模型（可配更优模型） |
| `ENCODER_INTEGRATION_API_KEY` | 主 API key | Integration Agent 专用 API key |
| `ENCODER_INTEGRATION_BASE_URL` | 主 base URL | Integration Agent 专用 API 地址 |
| `ENCODER_CHECKPOINT_ENABLED` | `1` | 启用断点恢复；设为 `0` 关闭（关闭后零副作用，不建目录） |
| `ENCODER_CHECKPOINT_DIR` | `.CHECKPOINT` | 断点数据根目录 |
| `ENCODER_CHECKPOINT_KEEP` | `10` | compaction 时保留的最近断点数 |
| `ENCODER_CHECKPOINT_MAX` | `50` | 断点数量上限，超过自动 compaction |

记忆系统和团队系统都以“失败可降级”为原则：低成本模型不可用时记忆回退到关键词路径，Mailbox 操作失败保留主流程，队友异常会记录到任务和结果消息中。例外：请求了 worktree 隔离但创建失败时**不会静默降级**——会明确提示“该 teammate 未隔离运行”，由 Lead 判断是否继续。

## 项目数据目录的边界

```text
.MEMORY/       跨回合的软记忆，可人工检查和维护
.TASK/         持久化任务、依赖、执行结果和归档
.Mailbox/      运行期间的 Agent 间消息队列
.worktrees/    可选的队友 Git 隔离工作区
.CHECKPOINT/   断点事件流与状态快照（按 session 分目录）
~/.encoder/tasks.json  每日定时任务
~/.encoder/tasks.log   定时任务执行日志
```

这些目录属于运行时数据，不应与原项目源码混为一谈。`.MEMORY/`、`.CHECKPOINT/` 已写入 `.gitignore`（`index.jsonl` 只是派生缓存，`archive/` 只是 compaction 归档，都不需要进版本库）；其余目录请按项目需要自行决定。

## 验证

新增能力分别有针对性测试，覆盖联网工具、记忆 v2、Todo/Task、Mailbox、队友状态机、并发、路径隔离、断点（事件日志容错与并发追加、触发策略、去重、parent 链、压缩前的 work_state 快照、恢复时的消息链修复、compaction 合并与归档、disabled 零副作用），以及 TUI 层的渲染纯函数与命令面板（无头模式）、回合被中断后的历史语义。运行完整测试：

```bash
pytest tests/ -q
```

也可以运行静态检查和字节码编译，TUI 离线冒烟（无需 API key）：

```bash
ruff check .
python -m compileall encoder
encoder --tui --demo
```
