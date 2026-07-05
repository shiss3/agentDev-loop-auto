# SDK 能力对照速查 — 开箱即用 / 需配置 / 自建

> **用途**: 开发时快速判断"这个能力 SDK 自带了吗？要不要自己写？"
> **SDK 版本**: claude-agent-sdk v0.2.93 | **更新**: 2026-06-28
>
> **星射线架构**：本项目采用三层星射线编排（L0 Architect / L1 Main / L2 Sub）。
> 对比角度围绕"三层能用到的能力"展开。

---

## 对比总览

| 分类 | SDK 开箱即用 | SDK 需代码配置 | 必须自建 |
|------|-------------|---------------|---------|
| **Agent 基础运行** | 核心 ReAct 循环、工具调用、流式输出 | 工具白名单、权限模式、模型选择 | — |
| **多人协作/编配** | SDK query() 单 Agent 执行 | 原生 Subagent 定义（`agents` 字段） | **星射线三层编排**（L0→L1→L2 的 DAG 调度、领域参数化、快车道/全编排路由） |
| **记忆与上下文** | SDK 自动压缩（CLI 子进程），session_id 续接 | 手动 resume、PreCompact Hook、get_context_usage() | **GCH**（全局上下文中枢 — MCP 工具 + SQLite 事实源），星型记忆拓扑 |
| **文件与状态安全** | SDK 查询级调用、`enable_file_checkpointing` | session_store 选择、permission_mode 控制 | **沙箱瞬间回滚**（Phase 3） |
| **UI 渲染** | — | — | **TUI/Chat 渲染**（Phase 1 已完成），三层编排进度展示 |
| **结构化输出** | `output_format`（json_schema） | — | — |
| **预算控制** | `task_budget`、`max_budget_usd`、`ResultMessage.total_cost_usd` | — | — |
| **Hooks 拦截** | — | PreToolUse / PostToolUse / PreCompact 等 Hook 注册 | 代码审查拦截、自动格式化（Phase 3） |
| **经验沉淀** | — | — | 挂起池 + Skill 知识库（Phase 4） |

---

## 第一象限：开箱即用 — 直接调用，不需要额外代码

SDK 拿到手就有的能力，Harness 直接使用。

| 能力 | SDK API | 星射线用途 | 自建成本 |
|------|---------|-----------|---------|
| **核心 Agent 循环** | `query(prompt, options)` | L0/L1/L2 所有 Agent 的执行入口 | 0 — 直接用 |
| **流式消息** | `AsyncIterator[Message]` | 实时渲染模型输出 | 0 — 已在 Phase 1 用 |
| **文本/工具/结果块** | `AssistantMessage.content`（`TextBlock`/`ToolUseBlock`）、`ResultMessage` | 区分"模型说话"和"模型动手" | 0 — 已在 Phase 1 用 |
| **结构化输出** | `output_format={"type": "json_schema", "schema": {...}}` | L0 Architect 返回结构化任务拆解、L1/L2 结构化报告 | 0 — Phase 2 直接启用 |
| **Budget 上限** | `task_budget=TaskBudget(total=...)` / `max_budget_usd=...` | L2 子 Agent 成本硬上限，超额自动停 | 0 — 配置即用 |
| **工具白名单过滤** | `allowed_tools=[...]` | **脑手解耦的硬约束**：L0/L1 只读，L2 可写 | 0 — 改配置即生效，Phase 2 P0 |
| **Session 续接** | `resume=session_id` / `continue_conversation=True` | L2 温启动"记得"上次改了什么 | 0 — SDK 内部恢复对话历史 |
| **Session Fork** | `fork_session(session_id, up_to_message_id=...)` | 从某检查点分叉并行尝试，互不污染 | 0 — SDK 原生支持 |
| **上下文用量查询** | `client.get_context_usage() → {percentage, isAutoCompactEnabled}` | 真实百分比驱动压缩决策 | 0 — 状态查询 |

### 使用示例

```python
from claude_agent_sdk import query, ClaudeAgentOptions, TaskBudget

# L2 子 Agent — 工具白名单+预算+结构化输出，全部开箱即用
opts = ClaudeAgentOptions(
    allowed_tools=["Read", "Write", "Edit", "Bash"],  # 脑手解耦硬约束
    task_budget=TaskBudget(total=8000),                # token 上限
    max_budget_usd=1.0,                                # 美元上限
    output_format={"type": "json_schema", "schema": {...}},  # 结构化输出
)
async for msg in query(prompt="实现该功能", options=opts):
    ...
```

---

## 第二象限：需要自己配置 / 集成 — SDK 提供了能力，但需代码激活

SDK 有对应的机制，但你需要写代码来配置、挂载或组合它们。

| 能力 | SDK 入口 | 配置要点 | 星射线落地 |
|------|---------|---------|-----------|
| **原生 Subagent** | `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` | 需自己定义 `AgentDefinition`（prompt + tools + model），注入 opts | L1→L2 委派通道（Phase 2 P1）。L1 用 `agents={...}` 定义子 Agent，SDK 自动管理 L2 执行循环 |
| **MCP 工具集成** | `mcp_servers: dict[str, MCPServerConfig]` | 需自己创建 MCP Server（`@tool` + `create_sdk_mcp_server`） | **GCH 总线**：L0/L1 通过 MCP 工具读写全局记忆；L2 不挂载 |
| **PreCompact Hook** | `hooks={"PreCompact": [HookMatcher(...)]}` | 需写 Hook 函数，注入附加上下文 | SD 压缩前抢救耐久事实写入 GCH（Phase 2 P2） |
| **session_store** | `session_store = FileSessionStore(path)` | 需实例化并注入 opts | L2 挂起池存储底座（Phase 2 P2） |
| **Pre/PostToolUse** | `hooks={"PreToolUse": [HookMatcher(...)]}` | 需写回调函数，按 agent_id/类型做准入判断 | 代码审查拦截（Phase 3）、危险工具护栏（Phase 2 P2 预演） |
| **生命周期子 Agent 钩子** | `SubagentStartHookInput` / `SubagentStopHookInput` | 需监听事件并发射给 renderer | 编排进度可视化（Phase 3） |
| **文件检查点** | `enable_file_checkpointing=True` | 开启即可，SDK 自动管理快照 | 干净重试（Phase 2 P2） |
| **session 分叉/标签** | `fork_session()` / `tag_session()` | 需在代码中调用 API 管理 | 挂起池管理（Phase 2 P2） |
| **长上下文 beta** | `betas=["context-1m-2025-08-07"]` | 需显式传入 options | L0 Architect 全局视野（Phase 2 P3） |

### 第三象限 与 第四象限 ： 需要自己编写全部逻辑

SDK 完全不提供，Harness 项目自己实现的机制。

| 能力 | Harness 实现 | 涉及阶段 |
|------|-------------|---------|
| **星射线三层编排** (DAG调度+领域参数化+快车道) | `core/orchestrator.py` + `core/dag.py` + `core/architect.py` + `core/main_agent.py` | Phase 2 P0 |
| **GCH 全局上下文中枢** | `core/gch/store.py` (SQLite) + `core/gch/server.py` (MCP Server) + `core/gch/schema.py` | Phase 2 P0 |
| **L2 子 Agent 执行+验收+重试** | `core/sub_agent.py` + `core/verify.py` — while 循环 retry | Phase 2 P0-P1 |
| **分层验收器** (确定性检查→LLM兜底) | `core/verify.py` — pytest/lint 优先，不调 LLM | Phase 2 P1 |
| **挂起池** (session 管理) | `core/pool.py` — SQLite 索引 + SDK session_store | Phase 2 P2 |
| **上下文用量监控+兜底** | `ContextGuard` — `get_context_usage().percentage` 驱动 kill/respawn | Phase 2 P2 |
| **危险工具护栏** (`can_use_tool` 预演) | `core/guard.py` — 按 agent_type 做准入判断 | Phase 2 P2 |
| **沙箱瞬时回滚** | PreToolUse Hook + 文件系统快照 (ESC 级别撤销) | Phase 3 |
| **代码审查拦截** | PreToolUse Hook — 自动审查变更 | Phase 3 |
| **Skill 知识库** | `.skills/` 目录 + SkillsAgent 自动检索注入 | Phase 4 |
| **TUI 全屏界面** | 基于 `prompt_toolkit` (已完成) | Phase 1 ✅ |
| **CLI 流式渲染** | `cli.py` + `TerminalRenderer` (已完成) | Phase 1 ✅ |
| **Chat REPL** | `chat/repl.py` + `chat/session.py` (已完成) | Phase 1 ✅ |

---

## 实战决策速查

> 开发时遇到的场景 → 查这里知道该用 SDK 还是自建。

| 场景 | 答案 |
|------|------|
| "想调一个 Agent 跑单次任务" | `query()` — 开箱即用 |
| "想让 L0 输出 JSON 格式的任务拆解" | `output_format` — 开箱即用 |
| "想让 L2 子 Agent 有 token 上限" | `task_budget` — 开箱即用 |
| "想约束 L0/L1 不能写文件" | `allowed_tools=["Read","Glob","Grep"]` — 开箱即用 |
| "想让 L2 恢复上次的上下文" | `resume=session_id` — 开箱即用 |
| "想检查当前上下文是否快满了" | `get_context_usage().percentage` — 开箱即用 |
| "想让子 Agent 有专属 system prompt 和工具集" | `ClaudeAgentOptions.agents` — SDK 提供了，需配置 `AgentDefinition` |
| "想挂载 GCH 让 L0/L1 读写全局记忆" | `mcp_servers={"gch": ...}` — SDK 提供了 MCP 通道，需自建 Server |
| "想在 SDK 自动压缩前保存关键信息" | `PreCompact` Hook — SDK 提供了，需写 Hook 函数 |
| "想实现 L0→L1→L2 三层编排调度" | **自建** — `dag.py` + `architect.py` + `main_agent.py` + `orchestrator.py` |
| "想实现 GCH 结构化记忆存储" | **自建** — `core/gch/store.py` (SQLite) + `core/gch/server.py` (MCP 工具) |
| "想实现检查/重试循环" | **自建** — `sub_agent.py` while 循环 + `verify.py` |
| "想实现 TUI 动画和富文本界面" | **自建** — `prompt_toolkit` 渲染 (已完成) |
| "想实现文件的沙箱回滚" | **自建** — Phase 3，PreToolUse Hook + 文件快照 |
