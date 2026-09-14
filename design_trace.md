参考优秀开源Agent设计，先讨论，再设计trace

trace链路观测！

Trace 本身只是“这次任务的容器”，真正描述任务内部发生了什么的，是一系列 Span。就是trace中装着的是spaw（跨度）信息，记录了Agent的每一步。

    1. 当前是谁执行？在做什么？
    2. 时间信息start-end？
    3. 输入/输出？
    4. 上下文元数据：当时环境是什么？
    5. 状态变化，成功与否？
    6. 各种执行依赖？

包含【谁 → 在什么时候 → 因为什么 → 调用了什么 → 输入什么 → 输出什么 → 成没成功 → 在什么环境 → 当时是什么状态。】

        但你设计 Agent Trace 时，最重要的是一个问题：
        不是“Span 要有哪些字段？”，而是“未来我要拿 Trace 回答哪些问题？”

        例如你的 Coding Agent 最少应该能回答：

        1. Agent 做了什么？
        2. Agent 为什么做这个动作？
        3. 哪个 LLM 决定了这个动作？
        4. 哪个 Tool 真正执行了？
        5. 执行花了多久？
        6. 为什么失败？
        7. 修改了哪些文件？
        8. 在哪个 Workspace / Sandbox？
        9. 当时 Agent State 是什么？
        10. 能不能从当时的 Checkpoint 恢复？

        然后倒推 Span：

        问题
        ↓
        需要什么信息
        ↓
        Span 字段



那我们需要加入那些字段呢？

    name：当前是那个agent执行
    当前的task/todo/review...
    workspace：当前的执行环境（文件路径/git worktree分支/sandbox...）
    tool_name/input/output
    description：当前动作的简要描述
    context：附加的上下文（一定限制，不因过多）
    start_time：开始时间
    end_time：结束时间
    各种状态的变化（比如task从pending ———> in progress ———> completed）
    执行后，变化的内容（file/code/...）
    产生的error
    当前操作是否涉及user-approval


