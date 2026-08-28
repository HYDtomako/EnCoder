# CoreCoder — CLI Coding Agent 学习笔记
参考 https://github.com/he-yufeng/CoreCoder

> 一份关于从零构建命令行编程智能体（CLI Coding Agent）的核心设计笔记，涵盖 **Agent Loop、Tool 体系、Context Engineering、并行执行与子 Agent、CLI 安全** 五大主题。
>
> 适用协议：OpenAI Function Calling

---

## 目录

- [01 · Agent Loop 执行循环](#01--agent-loop-执行循环)
- [02 · Tool 定义与 Edit_tool 设计](#02--tool-定义与-edit_tool-设计)
- [03 · Context Engineering 三级压缩](#03--context-engineering-三级压缩)
- [04 · 并行执行与子 Agent](#04--并行执行与子-agent)
- [05 · CLI 展示与会话安全](#05--cli-展示与会话安全)

---

## 01 · Agent Loop 执行循环

用 `for` 循环驱动整个作业流程，`max_rounds = 50` 防止无限迭代：

```python
for round in range(max_rounds):   # max_rounds = 50
    ...
```

**流转条件**：当前回合是否存在 **tool call**。有则继续循环，无则结束。

### 消息协议

Tool 调用遵循 OpenAI Function Calling 规范，消息链为：

```
assistant ──tool_call──▶ tool ──tool_call_id + tool_content──▶ assistant
```

### Tool 输出的 error 分类

一个场景：用户输入后，tool 的输出包含 error —— 需要区分它属于哪类问题：

| 类型 | 说明 |
| --- | --- |
| **① 参数传递 error** | 参数在传入 tool 函数时即失败 |
| **② 执行期 error** | tool 函数执行过程中抛出的异常 |

### Ctrl+C 中断的边界情况

用户按 `Ctrl+C` 时，若恰好截断在「模型调用 tool」与「tool 执行中」之间，会导致 **tool 的输出内容对应不到 `tool_call_id`** —— 对模型而言，tool 执行了但没有内容，这次中断会污染整个会话。

**解法**：补一条 `content: ["interrupt"]`，让历史消息重新合法，再把异常向上抛交给上层处理。

```python
# 中断修复示意
messages.append({
    "role": "tool",
    "tool_call_id": pending_call_id,
    "content": ["interrupt"],   # 修复断裂的消息链
})
raise InterruptedError(...)     # 交给上层
```

---

## 02 · Tool 定义与 Edit_tool 设计

### Tool 基类

Tool 定义继承于 `Tool` 基类，由 `schema()` 拼接成 OpenAI Function Calling 格式并注册进 `tool_list`：

```python
class Tool:
    def __init__(self):
        self.name = ...          # 工具名
        self.description = ...   # 工具描述
        self.parameters = ...    # JSON Schema 参数

    def execute(self, **kwargs):
        ...                      # 执行函数

    def schema(self) -> dict:
        ...  # 拼接成 OpenAI Function Calling 格式 → tool_list
```

### Edit_tool：文件修改的三种方案对比

| 方案 | 问题 |
| --- | --- |
| **a. 整文件重写** | 太浪费 token |
| **b. 按行号定位修改** | 模型对行数比较模糊，且行数改动不能有偏差 |
| **c. diff / patch 格式** | 让模型生成带 `@@ -42,7 +42,8 @@` 行号头的统一 diff，但错误率高得让人头疼 —— 那套上下文行数和偏移量它算不准 |

**最终方案 —— 唯一定位法**：

1. 定位【唯一修改内容】，让其**恰好出现一次**；
2. 若修改内容在文件中出现多次 → **增加上下文**，使其唯一出现；
3. **闭环验证**：tool 返回修改内容的 diff，交回模型自行判断是否正确。

### 严格的 Bash 执行

- **危险命令检测**：每个命令执行前先过一遍危险命令检测表（`rm` 等），命中即拦截；
- **cd 语义**：`cd a && cd b` 这种多级 cd 中，`b` 的工作目录是基于 `a` 的 —— 解析时需保持链式语义，不能孤立看待。

---

## 03 · Context Engineering 三级压缩

三级递进的压缩保障策略（从轻到重）：

### a. Tool 输出裁剪

`tool_call` 的返回值只在当时任务有用，后续任务几乎无可保留 —— 因此**保留首尾、删除中间内容**。

### b. 历史摘要压缩

对历史消息压缩成摘要，但**保留结构化内容**（tool 调用链、关键结果），而不是模糊的背景文本。

### c. 兜底截断

如果以上压缩仍不够，直接**只保留前几轮内容 + 摘要**，大幅度删除。

### ⚠️ 删除的坑：消息链断裂

如果删除的位置恰好落在 `tool_call` 处，`tool_id` 与 `tool_content` 就被分割开了（与 [01 章中断问题](#ctrlc-中断的边界情况)同源）。

**解法**：**移动分割边界**，保证 tool 消息对不被切断。

---

## 04 · 并行执行与子 Agent

### 流式并行 vs 顺序执行

- **Claude**：解析用户输入时，tools 已经在执行了（流式并行），不必等整段响应结束，回复更快；
- **CoreCoder**：老老实实地顺序执行。

### 多 tool 响应的并行调度

一个 agent 对任务的响应可能包含多个 tool 调用：

```
单个 tool  → 直接执行
多个 tool → 交给 _exec_tools_parallel（线程池）
```

**并行的隔离问题**：并行不能肆无忌惮 —— 当线程 1 正在保存 A 目录时，线程 2 并行保存 B，A 的内容就可能被覆盖。

**解法**：为不同线程相互隔离。Python 现成的工具是 `threading.local()` —— 它给每个线程一份独立的副本，线程之间互不可见：

```python
import threading

local = threading.local()
local.workspace = ...   # 每线程独立，互不可见
```

### 子 Agent 的处理

- **没有显式定义子 agent**，而是通过 `AgentTool` 定义：AgentTool 会**指回主 agent**，子 agent 能与其共享资源；
- 为子 agent **单独开 context**，让主 agent 窗口保持干净 —— 只需要子 agent 的执行结果；
- **子 agent 不能再调用 AgentTool**（防止无限嵌套）。

---

## 05 · CLI 展示与会话安全

### 展示与输出解耦

在 CLI 中做展示时，**agent 的实际输出与 UI 展示无关** —— 二者解耦，UI 层只负责渲染。

### `/save` 命令的路径安全

保存文件时输入 `<会话名>`，但命令行上可以敲**任意字符**（包括 `../../` 之类），存在目录穿越风险。设两道防线：

**第一道防线 —— 取末尾命名**

直接取路径的末尾命名，防止跳跃至其他目录：

```python
name = user_input.split("/")[-1].split("\\")[-1]
```

**第二道防线 —— 父目录校验**

通过比较 `path.parent == root`，确保最终落点仍在根目录之下：

```python
save_path = root / name
assert save_path.parent == root   # 例如 /home/user 必须等于 root
```

---

## 附录 · 核心要点速查

| 主题 | 一句话总结 |
| --- | --- |
| Agent Loop | `for` 循环 + `max_rounds=50`，流转条件 = 是否有 tool call |
| 中断处理 | `content: ["interrupt"]` 修复断裂的 tool 消息链 |
| Edit_tool | 唯一定位法：恰好一次出现 + 增加上下文 + diff 回验 |
| Bash 安全 | 命令先过危险命令检测表；cd 链式语义 |
| Context 压缩 | 裁剪 tool 输出 → 结构化摘要 → 前几轮 + 摘要兜底 |
| 并行隔离 | `threading.local()` 保证线程工作区互不可见 |
| 子 Agent | AgentTool 定义、独立 context、禁止嵌套调用 |
| 会话安全 | 两道防线：末尾命名 + `path.parent == root` |
