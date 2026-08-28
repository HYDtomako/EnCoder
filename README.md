# EnCoder

design_process/corecoder-note里有一些设计过程/学习心得
设计思想参考 https://github.com/shareAI-lab/learn-claude-code

这份文档只介绍 CoreCoder 在原项目基础上的新增能力。原项目的基础 Agent loop、模型适配、基础文件工具、上下文压缩和会话功能，请参阅 https://github.com/he-yufeng/CoreCoder。

## 新增能力概览

当前版本围绕“让 Agent 能持续推进复杂工作”增加了五组能力：

| 能力 | 解决的问题 | 持久化位置 |
| --- | --- | --- |
| Todo | 当前请求如何拆成轻量步骤并跟踪进度 | 内存，仅当前会话 |
| Task | 如何保存可恢复、可依赖、可派发的工作单元 | `.TASK/` |
| Memory v2 | 如何保存、召回、更新和维护跨回合信息 | `.MEMORY/` |
| Agent teammate | 如何让多个常驻 Agent 独立上下文并行协作 | `.Mailbox/`，可选 `.worktrees/` |
| 定时触发器 | 如何按每日计划自动提交 Agent 请求 | `~/.corecoder/tasks.json` |

此外新增了 Tavily 联网搜索和网页正文读取工具。所有新增工具都严格继承 `corecoder/tools/base.py` 中的 `Tool` 基类，并把可恢复错误作为工具结果返回，不让单个工具异常打断主循环。

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
~/.corecoder/tasks.json
```

触发时间使用本地时区的 `HH:MM` 格式。后台调度线程每 30 秒检查一次时钟；同一任务每天最多触发一次。触发器只负责检查时间并把到期任务放入队列，真正的模型调用仍在主线程执行，因此不会打断当前正在进行的 Agent 请求。

在 REPL 中，Agent 可以通过 `create_schedule` 创建每日任务，用户可以用以下命令查看和删除：

```text
/crontab                         查看全部定时任务
/crontab delete <task_id>        删除定时任务
```

默认交互模式会在用户输入间隙执行到期任务。如果需要让定时任务在没有交互输入时也持续运行，可以启动后台模式：

```bash
corecoder --daemon
```

后台模式会读取已有任务、等待触发并执行；执行结果同时记录到 `~/.corecoder/tasks.log`，方便事后查看。退出交互模式或 daemon 时，调度线程会被正常停止。

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

当并行修改存在文件冲突风险时，可以为队友启用 Git worktree 隔离；worktree 不可用时会降级到当前工作目录，不会因此让 Lead 主流程崩溃。队友拥有独立的 context 和工具实例，但不会继承 `agent`、`dispatch_task` 及团队管理工具，防止递归派发和共享可变工具状态污染。

新增团队工具包括：

```text
spawn_teammate       collect_results       review_teammate
release_teammate     broadcast_notice
```

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
```

## 配置项

新增配置均可通过环境变量设置，也可以写入项目根目录的 `.env`：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CORECODER_MEMORY_ENABLED` | `1` | 启用记忆系统；设为 `0` 关闭 |
| `CORECODER_MEMORY_LLM` | 主模型 | 记忆语义判断使用的低成本模型 |
| `CORECODER_TEAM_ENABLED` | `0` | 是否启用 teammate 模式 |
| `CORECODER_TEAM_MAX` | `3` | 最大并行 teammate 数量 |
| `CORECODER_TEAM_WORKTREES` | `0` | 是否默认尝试 Git worktree 隔离 |
| `CORECODER_TEAM_MODEL` | 主模型 | teammate 使用的模型 |
| `CORECODER_TEAM_API_KEY` | 主 API key | teammate 专用 API key |
| `CORECODER_TEAM_BASE_URL` | 主 base URL | teammate 专用 API 地址 |

记忆系统和团队系统都以“失败可降级”为原则：低成本模型不可用时记忆回退到关键词路径，Mailbox 或 worktree 操作失败时保留主流程，队友异常会记录到任务和结果消息中。

## 项目数据目录的边界

```text
.MEMORY/       跨回合的软记忆，可人工检查和维护
.TASK/         持久化任务、依赖、执行结果和归档
.Mailbox/      运行期间的 Agent 间消息队列
.worktrees/    可选的队友 Git 隔离工作区
~/.corecoder/tasks.json  每日定时任务
~/.corecoder/tasks.log   定时任务执行日志
```

这些目录属于运行时数据，不应与原项目源码混为一谈。提交到版本库前，请按项目需要决定是否将它们加入 `.gitignore`。

## 验证

新增能力分别有针对性测试，覆盖联网工具、记忆 v2、Todo/Task、Mailbox、队友状态机、并发和路径隔离。运行完整测试：

```bash
pytest tests/ -q
```

也可以运行静态检查和字节码编译：

```bash
ruff check .
python -m compileall corecoder
```
