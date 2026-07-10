# 原生 subagent（`AgentDefinition` + `agents`）

> 源码: `AgentDefinition` (`types.py:82`)，`ClaudeAgentOptions.agents` (`types.py:1794`)

## `AgentDefinition` 字段

```python
@dataclass
class AgentDefinition:                              # types.py:82
    description: str                                # 必需
    prompt: str                                     # 必需
    tools: list[str] | None = None                  # 已弃用传 "Skill"，用 skills
    disallowedTools: list[str] | None = None
    model: str | None = None                        # "sonnet"/"opus"/"haiku"/"inherit" 或完整 model ID
    skills: list[str] | None = None                 # ← subagent 也能挂 skills
    memory: Literal["user","project","local"] | None = None
    mcpServers: list[str | dict[str, Any]] | None = None   # server 名或 inline 配置
    initialPrompt: str | None = None
    maxTurns: int | None = None
    background: bool | None = None
    effort: EffortLevel | int | None = None
    permissionMode: PermissionMode | None = None
```

## 用法

```python
options = ClaudeAgentOptions(agents={
    "code-reviewer": AgentDefinition(
        description="审查代码变更",
        prompt="你是一个代码审查员...",
        tools=["Read", "Grep", "Glob"],
        model="sonnet",
    ),
})
# 主模型通过内置 "Agent" 工具调用 "code-reviewer"，SDK 自动管理其执行循环
```

## 星射线落地说明（⚠️ 当前不用）

星射线 L0 Full mode 用 `claude -p` **CLI 子进程**（独立 worktree）跑领域任务，**不用** SDK 原生 `agents`。原因: CLI 子进程天然提供进程级隔离 + worktree 隔离 + 独立 stdout 解析，符合 Full mode 的并行合并设计。

SDK 原生 `agents` 是未来 L1->L2 委派的备选通道（若 L1/L2 改为进程内 subagent 而非 CLI 子进程时启用）。当前 Phase 2 不启用，但保留 `AgentDefinition` 定义供 Phase 3+ 评估。
