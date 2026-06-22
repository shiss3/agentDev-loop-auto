# Harness Agent — 项目阶段总结与 Phase 2 编排计划

> **版本**: v4.0（SDK 能力对齐版） | **日期**: 2026-06-21 | **架构代号**: Star-Ray (星射线)
> **当前阶段**: Phase 1 已完成，准备进入 Phase 2
> **v3.0 修订要点**: 对齐 `claude-agent-sdk` v0.2.93 原生能力，修正 LangGraph 单一 State / Send / reducer 误用，补全反向瀑布流连线与收口节点，重定义压缩/温启动机制，增加依赖 DAG 与复杂度快车道。
> **v4.0 增量**（见 §4.6/4.7）: 基于实测 SDK 能力的二轮优化——`allowed_tools` 硬约束脑手解耦、`output_format` 结构化输出免解析、`rewind_files`/`fork_session` 干净重试、预算观测、L2 双路线决策、`can_use_tool` 护栏、thread_id/session_id 概念厘清，并修正 `get_context_usage` API 误用。新增机制→SDK API 映射对照表。

---

## 1. 项目概述

**Harness Agent** 是一个基于 `claude-agent-sdk` 和 `LangGraph` 的智能开发编排系统，采用**三层分形放射状 (3-Tier Fractal Radial)** 星射线架构，解决以下核心痛点：

| # | 痛点 | 星射线解决方案 | 目标阶段 |
|---|------|---------------|---------|
| 1 | 上下文爆炸 | 单向瀑布流 + 反向摘要 + SDK 原生压缩 | Phase 2 |
| 2 | 状态管理混乱 | LangGraph Checkpointer + SDK session 续接 | Phase 2 |
| 3 | 能力浪费 | 脑手解耦：主脑规划 + 子Agent执行 | Phase 2 |
| 4 | 缺少代码审查 | Hooks（PreToolUse）自动拦截 | Phase 3 |
| 5 | 经验无法沉淀 | 挂起机制 + .skills 知识库 | Phase 4 |

---

## 2. 项目路线图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Harness Agent 开发路线图 (星射线架构)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 1 ✅         Phase 2 🎯          Phase 3           Phase 4          │
│  ─────────         ─────────           ─────────         ─────────         │
│  单 Agent MVP      星射线编排           Hooks + 沙箱      经验沉淀           │
│                    (三层分形架构)        代码质量保障      (.skills)          │
│                                                                             │
│  ▼                 ▼                    ▼                ▼                  │
│                                                                             │
│  SDK 连通          👑 Architect         PreToolUse Hook   SkillsAgent       │
│  LangGraph 直线    🛡️ Main Agents       代码审查拦截      错误捕获 → 生成     │
│  CLI 流式渲染      ⚔️ Sub Agents        Sandbox 沙箱      .md 文件           │
│  Chat REPL         动态衍生(子图嵌套)    MCP Tools         知识库检索         │
│  TUI 全屏界面      记忆瀑布流(双向连线)  自动验证          注入 Agent         │
│                    SDK session 续接                                          │
│                    依赖 DAG + 快车道                                          │
│                                                                             │
│  Phase 5 (前端)    Phase 6 (优化)                                            │
│  ─────────────    ─────────────                                              │
│  Web + Flutter     向量检索、性能优化                                         │
│  可视化仪表盘      自适应策略                                                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1 完成情况

### 3.1 已完成功能

| 功能模块 | 状态 | 说明 |
|---------|------|------|
| **SDK 连通** | ✅ | `claude-agent-sdk` 集成完成，支持 `query()` 和 `ClaudeSDKClient` |
| **LangGraph 编排** | ✅ | 极简直线图 `START → default_agent → END` |
| **CLI 单次执行** | ✅ | `harness run "任务"` 支持流式渲染 |
| **Chat REPL** | ✅ | `harness chat` 多轮对话，支持历史会话恢复 |
| **TUI 全屏界面** | ✅ | 基于 `prompt_toolkit` 的全屏终端应用 |
| **流式渲染** | ✅ | 基于 `adispatch_custom_event` 的自定义事件流 |
| **斜杠命令** | ✅ | `/help`, `/exit`, `/clear`, `/stats`, `/context`, `/model`, `/undo` |
| **会话恢复** | ✅ | `--continue` 恢复最近会话，`--resume <id>` 恢复指定会话 |
| **取消请求** | ✅ | ESC 键取消当前请求 |
| **上下文接口** | ✅ | `ContextProvider` 协议定义，支持 Claude Code preset |

### 3.2 关键现状约束（Phase 2 设计的事实前提）

> ⚠️ 来自 `base_agent.py` 的现状，直接决定 Phase 2 哪些机制可行：

1. **真正的对话历史在 SDK 内部，不在 LangGraph 的 `state["messages"]`。**
   现有 `BaseAgent.__call__` 只从 state 取 `task` / `project_dir`，调用 `query()`；SDK 自己跑 ReAct 循环，`state["messages"]` 仅用于**归档最终结果**，不会回喂给模型。
   → **推论**：任何"压缩 `state["messages"]`"的机制对真实 token 占用无效。压缩/续命必须作用于 **SDK 真实 session**。

2. **流式渲染走 `adispatch_custom_event`（`on_custom_event`），不是 LangChain model 事件。**
   → **推论**：Phase 2 不能监听 `on_chat_model_stream` / `on_tool_start`，必须沿用 custom event。

3. **`query()` 是一次性调用**，无跨调用记忆；持久会话需改用 `ClaudeSDKClient` 或 `resume`/`session_id`。

### 3.3 SDK 原生能力盘点（v0.2.93，必须优先复用）

> 经实测，`claude-agent-sdk` v0.2.93 已原生提供以下能力，Phase 2 **复用而非重造**：

| 能力 | SDK 入口 | 在星射线中的用途 |
|------|---------|-----------------|
| **原生 Subagent** | `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` | 主智能体把领域子任务委托给 SDK 内置 subagent，免去自建执行循环 |
| **Session 续接** | `resume` / `session_id` / `continue_conversation` | 子Agent"温启动"的真正实现：恢复 SDK 侧真实对话历史 |
| **Session Fork** | `fork_session` / `ForkSessionResult` | 从某个检查点分叉出并行尝试，互不污染 |
| **上下文用量** | `ContextUsageResponse` / `ContextUsageCategory` | 用 SDK 真实 token 计数驱动压缩决策，替代手动 `count_tokens` |
| **原生压缩** | `PreCompactHookInput` / `fold_session_summary` | 在 SDK 即将压缩时介入，注入"核心经验记忆" |
| **预算控制** | `task_budget` / `max_budget_usd` / `TaskBudget` | 子Agent成本硬上限，超额即停 |
| **文件检查点** | `enable_file_checkpointing` | 子Agent改文件可回滚，为 Phase 3 验收/打回铺路 |
| **Session Store** | `session_store` / `SessionStore` | 子Agent会话持久化与列举（挂起池的存储底座） |
| **Subagent 钩子** | `SubagentStartHookInput` / `SubagentStopHookInput` | 监听 subagent 生命周期，发射事件给 renderer |

**设计原则更新**：星射线的"三层"是**编排语义层**（谁规划、谁执行、谁验收），而**执行与上下文管理尽量下沉到 SDK 原生能力**。LangGraph 负责"图拓扑 + 路由 + 跨 agent 状态聚合"，SDK 负责"单个 agent 的 ReAct 循环 + 会话 + 压缩"。两层职责不重叠。


---

## 4. Phase 2 目标：星射线编排系统

### 4.1 核心架构：三层分形放射状拓扑（子图嵌套版）

> **v3.0 关键修正**：LangGraph **一张图只有一个 state schema**。三层不同 State 不能塞进同一张扁平图。正确落地是 **subgraph 嵌套**——这恰恰是"分形"的本义：

```
主图 (ArchitectGraph, state=OrchestratorState)
│
├── node: architect            # L0 规划 + 拆解 + 依赖DAG
├── node: main_agent_subgraph  # ← 这是一张【编译好的子图】，作为单个节点
│       │
│       内部子图 (MainAgentGraph, state=MainAgentState)
│       ├── node: design            # 领域架构设计
│       ├── node: schedule          # 子任务排期 + 生成执行提示词
│       ├── node: sub_agent_subgraph # ← 又是一张子图（再分形）
│       │       │
│       │       内部子图 (SubAgentGraph, state=SubAgentState)
│       │       ├── node: execute   # 调用 SDK 执行（编码/测试）
│       │       ├── node: verify    # 确定性检查 + 可选LLM评审
│       │       └── node: summarize # 生成精炼摘要（反向瀑布流起点）
│       │
│       └── node: aggregate         # 聚合子Agent摘要 → 上报
│
└── node: finalize             # L0 收口：汇总所有领域 → 给用户统一答复
```

```
                              ┌──────────────────────────────────────┐
                              │           👑 核心节点                  │
                              │      Architect Agent (入口架构师 L0)   │
                              │  • 单一入口，接收用户宏观需求            │
                              │  • 全局项目目录视野                     │
                              │  • 任务拆解 + 依赖 DAG 构建             │
                              │  • 复杂度判定（快车道 / 全编排）         │
                              │  • 为主智能体编写专属提示词              │
                              │  • ❌ 绝对不写代码，不调用底层API        │
                              └───────────────────┬──────────────────┘
                          按依赖DAG拓扑分批 Send (非无脑全并行)
                              ┌───────────────────┼───────────────────┐
                              ▼                   ▼                   ▼
              ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
              │  🛡️ 主智能体子图 A   │ │  🛡️ 主智能体子图 B   │ │  🛡️ 主智能体子图 C   │
              │  domain=frontend    │ │  domain=backend     │ │  domain=database    │
              │  (同一参数化类)      │ │  (同一参数化类)      │ │  (同一参数化类)      │
              │  • 领域架构/契约     │ │  • 领域架构/契约     │ │  • 领域架构/契约     │
              │  • 子Agent工厂       │ │  • 子Agent工厂       │ │  • 子Agent工厂       │
              │  • 验收(确定性优先)  │ │  • 验收(确定性优先)  │ │  • 验收(确定性优先)  │
              │  • ❌ 不写代码       │ │  • ❌ 不写代码       │ │  • ❌ 不写代码       │
              └──────────┬──────────┘ └──────────┬──────────┘ └──────────┬──────────┘
                         │ Send(按子任务并行)     │                       │
              ┌──────────┼──────────┐ ┌──────────┼──────────┐ ┌──────────┼──────────┐
              ▼          ▼          ▼ ▼          ▼          ▼ ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐ ...（⚔️ Sub/Test Agent：SDK 执行单元）
         │⚔️ Sub  │ │⚔️ Sub  │ │⚔️ Test │
         │ execute│ │ execute│ │ verify │
         └───┬────┘ └───┬────┘ └───┬────┘
             │ 精炼摘要(reducer 聚合，非覆盖)
             ▼
   aggregate(L1) ──上报──> finalize(L0) ──> 统一答复给用户
```

### 4.2 三层节点职责

| 层级 | 节点类型 | 生命周期 | 核心职责 | 限制 |
|-----|---------|---------|---------|------|
| **L0** | 👑 Architect | 贯穿整个会话 | 入口路由、任务拆解、**依赖 DAG 构建**、**复杂度判定（快车道/全编排）**、全局视野维护、为主智能体写专属提示词、**收口汇总给用户** | ❌ 不写代码，不调用底层API |
| **L1** | 🛡️ Main Agent（参数化，按 `domain` 实例化） | 随 Session 存在 | 领域架构设计、接口契约、子任务排期、子Agent工厂、写执行提示词、**验收（确定性检查优先 + LLM 评审兜底）**、聚合上报 | ❌ 不写代码，不改文件 |
| **L2** | ⚔️ Sub/Test Agent（SDK 执行单元） | 动态衍生，完成后挂起，超预算/超阈值销毁 | 编码、文件读写、测试验证、生成精炼摘要 | ✅ 纯执行，无规划权 |

> **v3.0 修正**：删除 `frontend_main.py / backend_main.py / database_main.py` 三个硬编码文件。`MainAgent` 是 `domain` 参数化的**单一类**，领域通过**配置注册表**（`domains.yaml` 或 dict）声明。新增领域 = 加一行配置，**编排层代码零修改**——这才兑现"极致简洁"的卖点。

### 4.3 核心机制（修正版）

#### 4.3.1 动态衍生与依赖感知路由（Dynamic Spawning + DAG）

> **v3.0 修正三处**：① 路由函数**直接返回 `list[Send]`**，不经 `state["sends"]` 中转；② `add_conditional_edges` 用 Send 时**不需要 mapping dict**；③ 按**依赖 DAG 拓扑分批**发射，而非无脑全并行（前端依赖后端契约、后端依赖DB schema）。

```python
from langgraph.types import Send

def route_to_main_agents(state: OrchestratorState) -> list[Send]:
    """L0 → L1 路由：按依赖 DAG 的当前可执行批次发射。

    只发射"依赖已满足"的领域任务；下游领域等上游契约就绪后，
    在后续 super-step 由 aggregate 回流再次触发。
    """
    ready = state["dag"].ready_batch(done=state["completed_domains"])
    return [
        Send("main_agent_subgraph", {
            "agent_id": f"main_{t.domain}_{t.id[:8]}",
            "domain": t.domain,
            "global_snapshot": state["global_snapshot"],   # 只读快照
            "task": t,
            "main_prompt": state["main_agent_prompts"][t.domain],
        })
        for t in ready
    ]

# 注意：节点函数本身只返回 state 增量；Send 由这个独立路由函数产出
graph.add_conditional_edges("architect", route_to_main_agents)
```

> **复杂度快车道**：`architect` 先判定复杂度。单领域小改动 → 直接退化为 Phase 1 单 Agent（`Send("fast_lane", ...)`），跳过三层开销。只有跨领域/大型需求才走完整编排。

#### 4.3.2 并行写 State 必须有 Reducer（关键修正）

> **v3.0 必修**：多个 `main_agent` / `sub_agent` 在同一 super-step 并行写同一字段（如 `execution_summaries`、`completed_domains`），若 channel 无 reducer，LangGraph 抛 `InvalidUpdateError` 或互相覆盖。**所有被并行写入的字段都要声明 reducer**。

```python
import operator
from typing import Annotated, TypedDict

class OrchestratorState(TypedDict):
    user_request: str
    project_dir: str
    global_snapshot: GlobalSnapshot

    dag: TaskDAG
    decomposed_tasks: list[TaskSpec]
    main_agent_prompts: dict[str, str]

    # ↓↓↓ 并行写入字段：必须带 reducer ↓↓↓
    domain_reports: Annotated[list[DomainReport], operator.add]   # L1 聚合回流
    completed_domains: Annotated[list[str], operator.add]
    routing_history: Annotated[list[RoutingRecord], operator.add]

    final_answer: str   # 由 finalize 单点写，无需 reducer
```

#### 4.3.3 记忆瀑布流：向下只读快照 + 向上摘要（双向都要连进图）

> **v3.0 修正**：原计划 `report_to_architect` 是孤儿方法，图里没有反向连线。新增 `aggregate`（L1 收口）与 `finalize`（L0 收口）节点，把反向瀑布流真正连成边。

```
① 向下（派发只读快照）       ② 向上（精炼摘要回流，reducer 聚合）
Architect ──global_snapshot──▶ Main      Sub ──summary──▶ aggregate(L1)
Main      ──domain_snapshot──▶ Sub       aggregate ──domain_report──▶ finalize(L0)
                                          finalize ──final_answer──▶ 用户
```

- **向下**：`GlobalSnapshot` / `DomainSnapshot` 是**不可变 dataclass**（`frozen=True`），子层只读，无权改主脑记忆。
- **向上**：子Agent**只回传摘要**（改了哪些文件、新增哪些接口、关键决策），冗长执行日志在 SDK session 内自生自灭，不进主脑 state。

#### 4.3.4 子Agent"温启动"——基于 SDK 真实 Session（重定义）

> **v3.0 重定义**：原计划"温启动携带鉴权逻辑记忆"在 `query()` 模型下**做不到**（query 一次性、无记忆）。真正可行的是借助 SDK 的 **session 续接**。

```python
from dataclasses import dataclass
from claude_agent_sdk import ClaudeAgentOptions, TaskBudget, ResultMessage, query

@dataclass
class RunResult:
    session_id: str | None
    last_user_msg_id: str | None      # OPT-3：rewind_files 锚点
    total_cost_usd: float             # OPT-4：真实成本

class SubAgentRunner:
    """子Agent执行器：维持真实可续接的 session。L2 是唯一可写层。"""

    def _opts(self, snapshot, task, resume: str | None = None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=None if resume else build_sub_prompt(snapshot, task),
            resume=resume,                                  # 温启动时恢复历史
            cwd=snapshot.project_dir,
            allowed_tools=SUB_AGENT_TOOLS,                  # OPT-1：唯一有写权
            permission_mode="acceptEdits",
            task_budget=TaskBudget(total=task.budget_tokens),  # OPT-4：预算硬上限
            max_budget_usd=task.budget_usd,                 # OPT-4：美元上限
            enable_file_checkpointing=True,                 # OPT-3：可回滚
            session_store=self.store,                       # 持久化，供挂起
        )

    async def cold_start(self, task: TaskSpec, snapshot: DomainSnapshot) -> RunResult:
        return await self._run(self._opts(snapshot, task), task.description)

    async def warm_start(self, session_id: str, new_task: TaskSpec,
                         snapshot: DomainSnapshot) -> RunResult:
        """温启动 = resume SDK 真实会话，模型真正'记得'上次写的代码"""
        return await self._run(self._opts(snapshot, new_task, resume=session_id),
                              new_task.description)

    async def _run(self, opts, prompt) -> RunResult:
        sid = last_uid = None; cost = 0.0
        async for msg in query(prompt=prompt, options=opts):
            sid = capture_session_id(msg) or sid
            last_uid = capture_user_msg_id(msg) or last_uid
            if isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0            # OPT-4：真实成本
            # 内部 adispatch_custom_event 发射事件（略）
        return RunResult(sid, last_uid, cost)
```

> **挂起池**不再用 LangGraph Checkpointer 当 KV 存任意对象（那是误用，Checkpointer 按 thread 存整图快照）。改用 SDK `session_store` + 一张轻量 SQLite 表记录 `{sub_agent_id → session_id, domain, status, last_active}`。

#### 4.3.5 上下文压缩——SDK 原生自动压缩为主，编排层兜底（重定义 v4.0）

> **v3.0 重定义 + v4.0 修正**：压缩 `state["messages"]` 无效（它不回喂模型）。改用 SDK 真实用量 + 原生压缩钩子。
> **v4.0 修正两处 API 误用**：① `get_context_usage()` 是 **`ClaudeSDKClient` 的实例方法**，不是自由函数——要监控用量必须持有**长连接 client**（`query()` 一次性调用拿不到）；② `ContextUsageResponse` 的字段是 **`percentage` / `totalTokens` / `maxTokens`**，没有 `.ratio`。
> **关键认知**：SDK **本身已内置自动压缩**（`ContextUsageResponse.isAutoCompactEnabled` + `autoCompactThreshold`）。所以编排层不必重造压缩，主策略是"**信任 SDK 自动压缩 + PreCompact 钩子注入领域记忆防丢**"，KILL 重建仅作为**最后兜底**。

```python
# 方案 A（首选）：信任 SDK 自动压缩；用 PreCompact 钩子在压缩前注入"核心经验"，防止关键领域上下文被压没
async def on_pre_compact(inp: PreCompactHookInput, tool_use_id, ctx: HookContext):
    # inp["trigger"] == "auto" | "manual"；inp["custom_instructions"] 可携带压缩指令
    # 可在此把领域契约/关键决策作为 custom_instructions 注入，引导压缩保留要点
    return {"hookSpecificOutput": {"hookEventName": "PreCompact",
                                   "additionalContext": domain_memory_digest()}}

options = ClaudeAgentOptions(hooks={"PreCompact": [HookMatcher(hooks=[on_pre_compact])]})

# 方案 B（编排层兜底，仅 L2 用 ClaudeSDKClient 长连接时可用）：真实用量驱动 KILL 重建
async def assess(client: ClaudeSDKClient) -> str:
    usage = await client.get_context_usage()       # 实例方法
    if usage["percentage"] > 90 and not usage["isAutoCompactEnabled"]:
        return "kill_and_respawn"                   # 摘要交接 → 新 session 冷启动
    return "continue"
```

> 文档措辞同步修正："**零幻觉**" → "**降低幻觉**"；废弃"60% 阈值压缩"提法，改为"**SDK 原生自动压缩 + PreCompact 注入领域记忆；编排层仅在禁用自动压缩或 `percentage` 逼近上限时 KILL 重建兜底**"。

#### 4.3.6 验收：确定性信号优先，LLM 评审兜底

> **v3.0 修正**：原计划验收全靠 LLM 读 diff 打勾，幻觉风险高、成本高。改为分层验收，并为 Phase 3 Hooks 预留接口。

```
子Agent 完成 ──▶ ① 确定性检查（机器可判定，零LLM成本）
                    • 测试命令是否通过（pytest / npm test）
                    • lint / 类型检查（ruff / mypy / tsc）
                    • 契约 schema 校验（接口定义比对）
                  ├─ 全绿 ──▶ 直接通过，不调 LLM
                  └─ 有红/无法机器判定 ──▶ ② LLM 评审（兜底）
                                            按 acceptance_criteria 评审 diff
```

#### 4.3.7 测试回环的重试上限（防 recursion_limit 撞墙）

> **v3.0 必修**：`verify --fail--> execute` 无上限会顶到 LangGraph `recursion_limit` 抛错。

```python
class SubAgentState(TypedDict):
    ...
    retry_count: int      # 每次打回 +1

def route_after_verify(state: SubAgentState) -> str:
    if state["verify_passed"]:
        return "summarize"
    if state["retry_count"] >= 3:
        return "escalate"   # 升级给 L1 主智能体：换方案 / 重设计 / 上报人类
    return "execute"        # 打回重写
```

### 4.4 Phase 2 不做的事（明确排除）

| 排除项 | 原因 |
|-------|------|
| ❌ Hooks 代码审查拦截（PreToolUse 完整审查） | Phase 3（但 OPT-6 的 `can_use_tool` 危险护栏 Phase 2 先上） |
| ❌ Sandbox 沙箱隔离 | Phase 3（SDK 已有 `sandbox` 字段，届时启用） |
| ❌ Skills 经验沉淀 | Phase 4 |
| ❌ Web/Flutter 前端 | Phase 5 |
| ❌ 主Agent写代码 | 违反"脑手解耦"——由 OPT-1 `allowed_tools` 工具层硬约束 |
| ❌ 静态领域硬编码文件 | 改为参数化 + 配置注册表 |
| ❌ 自建 subagent 执行循环 | 复用 SDK 原生 `agents` / `query`（见 OPT-5 路线决策） |
| ❌ 压缩 LangGraph `state["messages"]` | 无效（不回喂模型），改用 SDK 原生自动压缩 + 用量监控 |
| ❌ 手动 `count_tokens` 估算窗口 | 不准，改用 `get_context_usage().percentage` |

### 4.5 优先级排期表（含 v4.0 OPT 项）

> **v3.0 新增 / v4.0 扩充**：把基建难点与 §4.6 的 OPT 项统一排进 P0/P1/P2，确保主干 L0→L1→L2→收口 先跑通，SDK 增量优化按收益插入。

| 机制 | 优先级 | 来源 | 说明 |
|------|-------|------|------|
| L0→L1→L2 主干 + 收口 + 流式 | **P0 必做** | v3.0 | 没有它整个架构不成立 |
| 依赖 DAG 分批路由 | **P0 必做** | v3.0 | 否则下游基于不存在的契约编码 |
| reducer 并行聚合 | **P0 必做** | v3.0 | 否则并行即崩 |
| **`allowed_tools` 硬约束脑手解耦** | **P0 必做** | OPT-1 | 改一行配置即生效，收益极高、成本极低，应随主干一起做 |
| **`output_format` 结构化输出** | **P0 必做** | OPT-2 | 消除拆解/验收解析的高频 bug，Step 2 直接用 |
| **thread_id / session_id 概念厘清** | **P0 必做** | OPT-7 | 不厘清挂起池与会话恢复会写错，属设计前提 |
| 重试上限 + 复杂度快车道 | **P1 重要** | v3.0 | 健壮性与成本 |
| 确定性验收 | **P1 重要** | v3.0 | 降幻觉、降成本 |
| **L2 双路线决策（A 包 B）** | **P1 重要** | OPT-5 | 决定 Step 4 实现形态，需在编码前定调 |
| **预算纳入 TaskSpec + 成本观测** | **P1 重要** | OPT-4 | `task_budget` + `total_cost_usd`，防失控、可观测 |
| 挂起/温启动（session 续接） | **P2 可后置** | v3.0 | 先冷启动跑通，再加续命 |
| SDK 原生自动压缩 + 用量监控兜底 | **P2 可后置** | v3.0/§4.3.5 | 短任务暂不触发 |
| **`rewind_files`/`fork_session` 干净重试** | **P2 可后置** | OPT-3 | 依赖 `enable_file_checkpointing`，重试质量优化 |
| **`can_use_tool` 危险护栏** | **P2 可后置** | OPT-6 | 预演 Phase 3，主干稳定后再加 |
| **Architect 1M 上下文 beta** | **P3 按需** | OPT-8 | 仅大仓库且成本可接受时开 |

> **排期解读**：OPT-1/2/7 提到 **P0**——它们不是"锦上添花"，而是改动极小却直接决定正确性/健壮性的设计前提，应与主干同批落地。OPT-4/5 提到 **P1**，因为会影响 Step 4 的实现形态。OPT-3/6/8 仍可后置。

### 4.6 第二轮审视：基于 SDK 真实能力的增量优化（v4.0 新增）

> 这一轮在 v3.0 架构基础上，挖掘 SDK 既有能力能让计划"更省、更稳、更准"的地方。每条都标注**收益**与**落地 API**。

#### OPT-1 用 `allowed_tools` 在工具层"硬"约束脑手解耦（强烈建议）

> v3.0 靠 system_prompt 写"❌ 不写代码"来约束 L0/L1，这是**软约束**，模型仍可能违规。
> **优化**：L0/L1 的 `allowed_tools` 只给**只读工具**（`Read`/`Glob`/`Grep`），物理上拿不到 `Write`/`Edit`/`Bash`；写工具只发给 L2。约束从"提示词层"提升到"能力层"，无法被绕过。

```python
ARCHITECT_TOOLS = ["Read", "Glob", "Grep"]              # L0：只读全局
MAIN_AGENT_TOOLS = ["Read", "Glob", "Grep"]             # L1：只读领域
SUB_AGENT_TOOLS  = ["Read", "Write", "Edit", "Bash"]    # L2：唯一有写权
```

**收益**：脑手解耦从"君子协定"变"制度保证"，验收项 #2 可机器验证。

#### OPT-2 用 `output_format` + `structured_output` 替代文本解析（强烈建议）

> v3.0 的 `_plan()` 让模型输出 JSON 再 `parse_plan()` 手解析，脆弱（模型可能夹带 markdown 围栏/解释）。
> **优化**：设 `options.output_format` 声明 schema，结果直接落在 `ResultMessage.structured_output`，免解析、强类型。

```python
options = ClaudeAgentOptions(output_format={"type": "json_schema", "schema": ARCHITECT_PLAN_SCHEMA})
# 消费：
async for msg in query(prompt=..., options=options):
    if isinstance(msg, ResultMessage) and msg.structured_output:
        plan = msg.structured_output      # 已是结构化 dict，无需 parse
```

**收益**：消除 Architect 拆解 / 验收结果解析的一类高频 bug，适用于所有"要结构化结果"的节点。

#### OPT-3 验收失败重试前，用 `rewind_files()` / `fork_session()` 清理污染（建议）

> v3.0 的 `route_after_verify` 失败直接回 `execute`，但**失败的半成品代码和被污染的 session 历史还在**，重试是在脏地基上继续。
> **优化**：失败回退时，先 `client.rewind_files(user_message_id)` 把文件回滚到本次尝试前（需 `enable_file_checkpointing=True`），或用 `fork_session(up_to_message_id=...)` 从干净检查点分叉重来。

**收益**：重试基于干净状态，显著提升 3 次内修复成功率；避免错误代码层层叠加。

#### OPT-4 预算纳入 TaskSpec，超额走 escalate（建议）

> v3.0 提了 `task_budget` 但没和"重试/升级"联动。
> **优化**：把 `max_budget_usd` / `TaskBudget(total=...)` 写进每个子任务，SDK 超预算自动停；编排层从 `ResultMessage.total_cost_usd` 读真实花费，累计进领域报告。预算耗尽视同一次失败，计入 `retry_count`，触发 escalate。

**收益**：成本可控且可观测，反向瀑布流报告里能给用户真实 `$` 花费。

#### OPT-5 L2 执行单元的两种实现路线——需明确决策（重要）

> v3.0 把 L2 实现为"LangGraph 子图节点里调 `query()`"。但 SDK 还有**原生 `agents` 子 agent**路线。两者是真实的架构取舍，必须明确选一条：

| 路线 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A. LangGraph 子图 + `query()`**（v3.0 现选） | execute/verify/retry 都是 LangGraph 节点 | 编排层**完全掌控**重试/验收/分支；事件流统一 | 自己管 session/并发 |
| **B. SDK 原生 `agents`** | L1 把任务委托给 `AgentDefinition`，CLI 调度 | 上下文**天然隔离**；`SubagentStart/Stop` 钩子；省去自管 | 重试/验收逻辑被关进 CLI，LangGraph 失去细粒度控制 |

> **建议**：**主干用 A**（保留 LangGraph 对 verify→retry→escalate 闭环的控制，这是星射线的核心价值）；但 **L2 内部的"一次执行"可设 `agents` 让 CLI 自行拆解微任务**，即"A 包 B"。同时无论哪条，都注册 `SubagentStart/SubagentStop` 钩子发射层级事件给 renderer。

#### OPT-6 `can_use_tool` 回调：Phase 2 就能加的轻量准入（可选，提前预演 Phase 3）

> Phase 3 才做 PreToolUse 代码审查，但 SDK 的 `can_use_tool` 回调是**编程式**的、零成本，Phase 2 可先用它做一道**危险操作护栏**（如拦截 `rm -rf` / 写出领域目录外的路径），为 Phase 3 平滑铺路。

```python
async def guard(tool_name, tool_input, ctx) -> PermissionResult:
    if tool_name == "Bash" and is_destructive(tool_input.get("command","")):
        return {"behavior": "deny", "message": "危险命令被护栏拦截", "interrupt": False}
    if tool_name in ("Write","Edit") and escapes_domain(tool_input, ctx):
        return {"behavior": "deny", "message": "越界写入被拦截", "interrupt": False}
    return {"behavior": "allow", "updated_input": None, "updated_permissions": None}
```

#### OPT-7 两套"会话/线程"概念必须分清（修正歧义）

> 文档里 `thread_id`（LangGraph 图检查点）和 SDK `session_id`（CLI 侧对话）混用易致 bug：
> - **LangGraph `thread_id`**：整图状态的检查点键，`harness chat --resume` 恢复**编排进度**用这个。
> - **SDK `session_id`**：单个 L2 子 agent 的 CLI 会话，温启动续命用这个。
> 一个 `harness chat` 会话 = 1 个 LangGraph `thread_id` + N 个 SDK `session_id`（每个挂起子 agent 一个）。挂起池表里两者都要存。

#### OPT-8 Architect 全局视野可选开 1M 上下文（可选）

> L0 需要"全局项目目录视野"，大仓库快照可能很大。`betas=["context-1m-2025-08-07"]` 可让 Architect 用 1M 窗口，减少为压缩而做的激进裁剪。仅 L0 按需开启（成本更高），L2 执行单元无需。

### 4.7 机制 → SDK API 映射对照表（v4.0 新增）

| 星射线机制 | 落地 SDK 能力 | 具体 API / 字段 | 对应 Step |
|-----------|--------------|----------------|----------|
| L0/L1 脑手解耦（硬约束） | 工具白名单 | `allowed_tools`（只读集） | Step 2/3 |
| L0 任务拆解（免解析） | 结构化输出 | `output_format` → `ResultMessage.structured_output` | Step 2 |
| L0 全局视野 | 长上下文 beta | `betas=["context-1m-2025-08-07"]` | Step 2 |
| L1 委派 L2 | 原生子 agent（可选 B 路线） | `agents={...: AgentDefinition}` | Step 3/4 |
| L2 一次性执行 | 无状态调用 | `query(prompt, options)` | Step 4 |
| L2 温启动续命 | 会话续接 | `resume` / `ClaudeSDKClient` / `continue_conversation` | Step 5 |
| L2 干净重试 | 文件回滚 / 会话分叉 | `enable_file_checkpointing`+`rewind_files()` / `fork_session()` | Step 4 |
| 成本硬上限 + 观测 | 预算 | `max_budget_usd` / `task_budget` / `ResultMessage.total_cost_usd` | Step 4 |
| 上下文管理 | 原生自动压缩 + 钩子 | `isAutoCompactEnabled` / `PreCompact` hook | Step 6 |
| 用量监控（兜底） | 上下文用量 | `ClaudeSDKClient.get_context_usage()`→`percentage` | Step 6 |
| 挂起池存储 | 会话存储 + 管理 | `session_store` / `list_sessions` / `tag_session` | Step 5 |
| 子 agent 生命周期事件 | 子 agent 钩子 | `SubagentStart` / `SubagentStop` hook | Step 4/9 |
| 危险操作护栏（预演 P3） | 工具准入回调 | `can_use_tool` → `PermissionResult` | Step 4 |
| 自定义领域工具 | 进程内 MCP | `@tool` / `create_sdk_mcp_server` | 按需 |
| 流式渲染 | 内容块 → 自定义事件 | `AssistantMessage.content` blocks + `adispatch_custom_event` | Step 9 |
| 会话恢复（编排进度） | LangGraph 检查点 | `thread_id` + `AsyncSqliteSaver` | Step 8/9 |
| 异常兜底 | SDK 异常 | `CLINotFoundError` / `CLIConnectionError` 等 | 全局 |

> 详细 API 语义见 `.claude/CLAUDE.md`（SDK v0.2.93 能力速查）。

---

## 5. Phase 2 详细计划

### 5.1 模块变更清单（修正版）

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `core/state.py` | **重构** | 三层 State（带 reducer）：`OrchestratorState` / `MainAgentState` / `SubAgentState`；快照 dataclass `frozen=True` |
| `core/dag.py` | **新增** | `TaskDAG`：依赖图，`ready_batch()` 返回可执行批次 |
| `core/architect.py` | **新增** | L0：拆解 + DAG + 复杂度判定 + 收口 |
| `core/main_agent.py` | **新增** | L1：**单一参数化类**（按 `domain` 实例化），构建为子图 |
| `core/sub_agent.py` | **新增** | L2：`SubAgentRunner`（cold/warm start，SDK session） |
| `core/domains.yaml` | **新增** | 领域注册表（替代三个硬编码文件）：每领域含 `tools`（OPT-1 工具集）、确定性验收命令（如 `pytest`/`tsc`）、默认预算 |
| `core/pool.py` | **新增** | 挂起池：`{sub_agent_id → session_id}` + SDK `session_store`；区分 thread_id/session_id（OPT-7） |
| `core/context/snapshot.py` | **新增** | `GlobalSnapshot` / `DomainSnapshot`（frozen dataclass） |
| `core/context/summary.py` | **新增** | 反向摘要提炼器 |
| `core/verify.py` | **新增** | 分层验收：确定性检查 + LLM 兜底 |
| `core/schemas.py` | **新增** | `output_format` 结构化输出 schema（OPT-2）：`ARCHITECT_PLAN_SCHEMA` 等 |
| `core/guard.py` | **新增（P2）** | `can_use_tool` 危险操作护栏（OPT-6，预演 Phase 3） |
| `core/orchestrator.py` | **重构** | 子图嵌套：主图 + MainAgent 子图 + SubAgent 子图 |
| `chat/repl.py` | **修改** | ChatCLI 入口不变，底层切星射线；保留 `--continue/--resume` 映射到 thread_id |
| `chat/session.py` | **保留过渡** | 不立即退役；快车道仍复用其单 Agent 路径，待主干稳定再评估 |

### 5.2 任务分解

#### Step 1: 定义三层 State + 快照 + DAG（~3h）

```python
import operator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage, add_messages

# ── 只读快照：frozen，子层无权改主脑记忆 ──
@dataclass(frozen=True)
class GlobalSnapshot:
    project_dir: str
    project_structure: dict
    tech_stack: tuple[str, ...]        # tuple 而非 list，强化不可变

@dataclass(frozen=True)
class DomainSnapshot:
    domain: str
    project_dir: str
    architecture_design: str
    interface_contracts: tuple[Contract, ...]

# ── L0 ──
class OrchestratorState(TypedDict):
    user_request: str
    project_dir: str
    global_snapshot: GlobalSnapshot
    dag: TaskDAG
    decomposed_tasks: list[TaskSpec]
    main_agent_prompts: dict[str, str]
    complexity: str                                  # fast_lane | full
    # 并行写：带 reducer
    domain_reports: Annotated[list[DomainReport], operator.add]
    completed_domains: Annotated[list[str], operator.add]
    routing_history: Annotated[list[RoutingRecord], operator.add]
    final_answer: str                                # finalize 单点写

# ── L1 ──
class MainAgentState(TypedDict):
    agent_id: str
    domain: str
    global_snapshot: GlobalSnapshot                  # 继承只读
    main_prompt: str
    architecture_design: str
    interface_contracts: list[Contract]
    scheduled_tasks: list[TaskSpec]
    sub_agent_prompts: dict[str, str]
    acceptance_criteria: str
    # 并行写：带 reducer
    sub_summaries: Annotated[list[str], operator.add]
    completed_tasks: Annotated[list[TaskRecord], operator.add]

# ── L2 ──
class SubAgentState(TypedDict):
    agent_id: str
    parent_main_agent: str
    agent_type: str                                  # coding | test
    domain_snapshot: DomainSnapshot                  # 继承只读
    current_task: TaskSpec
    sub_prompt: str
    session_id: str | None                           # SDK 真实会话，用于温启动
    last_msg_id: str | None                          # OPT-3：rewind_files 回滚锚点
    cost_usd: Annotated[float, operator.add]         # OPT-4：真实成本累计（重试也累加）
    messages: Annotated[list[BaseMessage], add_messages]  # 仅归档
    verify_passed: bool
    retry_count: int                                 # 防回环
    status: str                                      # active|suspended|completed|killed|escalated
    created_at: datetime
    last_active_at: datetime
```

#### Step 2: 入口架构师 + 复杂度判定 + DAG（~5h）

> **v3.0 优化**：拆解与"生成 main_agent prompt"**合并为一次结构化输出**（一个 JSON 同时给任务、领域、依赖、边界、验收），把 L0 的 LLM 往返从 `1 + N` 压到 `1`。
> **v4.0 增强（OPT-1/OPT-2）**：① L0 用**只读 `allowed_tools`**（`Read`/`Glob`/`Grep`），工具层物理保证"不写代码"；② 用 **`output_format` + `ResultMessage.structured_output`** 拿结构化结果，**废弃脆弱的文本 `parse_plan()`**。

```python
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

ARCHITECT_TOOLS = ["Read", "Glob", "Grep"]          # OPT-1：L0 只读，拿不到 Write/Edit/Bash

# OPT-2：声明结构化输出 schema，免解析
ARCHITECT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "complexity": {"enum": ["fast_lane", "full"]},
        "tasks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "domain": {"type": "string"},
            "description": {"type": "string"}, "priority": {"type": "integer"},
            "depends_on": {"type": "array", "items": {"type": "string"}}}}},
        "prompts": {"type": "object"},               # {domain: "含①需求②边界③验收的Markdown"}
    },
    "required": ["complexity", "tasks", "prompts"],
}


class ArchitectAgent:
    """L0 入口架构师。❌ 不写代码——由 ARCHITECT_TOOLS 只读集硬约束。"""

    def __init__(self, model: str | None = None):
        self.model = model
        self.scanner = GlobalVisionScanner()

    async def __call__(self, state: OrchestratorState) -> dict:
        """注意：只返回 state 增量（dict）。Send 由独立路由函数产出。"""
        if not state.get("global_snapshot"):
            structure = await self.scanner.scan(state["project_dir"])
            tech = await self.scanner.detect_tech_stack()
            snapshot = GlobalSnapshot(state["project_dir"], structure, tuple(tech))
        else:
            snapshot = state["global_snapshot"]

        # 一次结构化调用：拆解 + 依赖 + 每领域提示词（含边界/验收）
        plan = await self._plan(state["user_request"], snapshot)

        return {
            "global_snapshot": snapshot,
            "decomposed_tasks": plan["tasks"],
            "dag": TaskDAG.from_tasks(plan["tasks"]),  # 依赖图
            "main_agent_prompts": plan["prompts"],     # {domain: prompt}
            "complexity": plan["complexity"],          # fast_lane | full
        }

    async def _plan(self, user_request, snapshot) -> dict:
        """OPT-1+OPT-2：只读工具 + 结构化输出，返回已校验的 dict（无需手解析）"""
        opts = ClaudeAgentOptions(
            allowed_tools=ARCHITECT_TOOLS,             # 只读，物理禁止写代码
            cwd=snapshot.project_dir,
            model=self.model,
            max_turns=1,
            output_format={"type": "json_schema", "schema": ARCHITECT_PLAN_SCHEMA},
        )
        prompt = f"""分析需求并按 schema 输出任务分解：
## 需求
{user_request}
## 项目结构
{format_structure(snapshot.project_structure)}
## 技术栈
{snapshot.tech_stack}

要求：
- complexity: "fast_lane"(单领域小改) | "full"(跨领域/大型)
- tasks: 每项含 id/domain/description/priority/depends_on
- prompts: {{domain: "含①任务需求 ②边界要求 ③验收标准 的Markdown"}}"""
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, ResultMessage) and msg.structured_output:
                return msg.structured_output           # 已是结构化 dict
        raise RuntimeError("Architect 未返回结构化结果")

    async def finalize(self, state: OrchestratorState) -> dict:
        """收口节点：汇总所有 domain_reports → 给用户统一答复（同样只读）"""
        opts = ClaudeAgentOptions(allowed_tools=ARCHITECT_TOOLS, model=self.model, max_turns=1)
        answer_parts = []
        async for msg in query(prompt=f"""将各领域完成情况汇总为给用户的最终答复：
{format_reports(state["domain_reports"])}
输出：总体完成情况 + 各领域要点 + 遗留事项 + 累计成本""", options=opts):
            answer_parts.append(extract_text(msg))
        return {"final_answer": "".join(answer_parts)}


# ── 路由函数：直接返回 list[Send]，按 DAG 分批；支持快车道 ──
def route_from_architect(state: OrchestratorState):
    if state["complexity"] == "fast_lane":
        return Send("fast_lane", {"task": state["decomposed_tasks"][0],
                                  "snapshot": state["global_snapshot"]})
    ready = state["dag"].ready_batch(done=state["completed_domains"])
    if not ready:
        return "finalize"          # 全部领域完成 → 收口
    return [Send("main_agent_subgraph", {
        "agent_id": f"main_{t.domain}_{t.id[:8]}",
        "domain": t.domain,
        "global_snapshot": state["global_snapshot"],
        "main_prompt": state["main_agent_prompts"][t.domain],
        "scheduled_tasks": [t],
    }) for t in ready]
```

#### Step 3: 主智能体子图（参数化，~7h）

> **v3.0 优化**：① 单一参数化类，领域来自配置；② 构建为**独立编译子图**作为主图节点；③ 验收走 `core/verify.py` 分层逻辑。
> **v4.0 增强（OPT-1）**：L1 同样用**只读 `allowed_tools`**，工具层保证"不写代码不改文件"；`design`/`_gen_sub_prompts` 用一次性 `query()`（无需长连接 client）。

```python
from claude_agent_sdk import ClaudeAgentOptions, query

MAIN_AGENT_TOOLS = ["Read", "Glob", "Grep"]         # OPT-1：L1 只读领域，无写权

class MainAgent:
    """L1 领域主理人。❌ 不写代码——由 MAIN_AGENT_TOOLS 只读集硬约束。按 domain 参数化。"""

    def __init__(self, domain: str, model: str | None = None):
        self.domain = domain
        self.model = model

    def _ro_opts(self, cwd: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(allowed_tools=MAIN_AGENT_TOOLS, cwd=cwd,
                                  model=self.model, max_turns=1)

    async def design(self, state: MainAgentState) -> dict:
        if state.get("architecture_design"):
            return {}
        parts = []
        async for msg in query(prompt=f"""你是{self.domain}领域架构师。
## 全局信息
{state['global_snapshot']}
## 任务
{format_tasks(state['scheduled_tasks'])}
只输出设计文档（模块划分/接口契约/数据流/选型），不要写代码。""",
                               options=self._ro_opts(state['global_snapshot'].project_dir)):
            parts.append(extract_text(msg))
        return {"architecture_design": "".join(parts)}

    async def schedule(self, state: MainAgentState) -> dict:
        """排期 + 一次性为所有子任务生成执行提示词"""
        prompts = await self._gen_sub_prompts(state)   # 合并为一次调用
        return {"sub_agent_prompts": prompts}

    async def aggregate(self, state: MainAgentState) -> dict:
        """L1 收口：聚合子Agent摘要 → 生成领域报告（反向瀑布流）"""
        report = DomainReport(domain=self.domain,
                              summaries=state["sub_summaries"],
                              completed=len(state["completed_tasks"]))
        return {"domain_reports": [report],            # reducer 聚合回 L0
                "completed_domains": [self.domain]}


def route_to_sub_agents(state: MainAgentState):
    """L1 → L2：按子任务并行 Send"""
    return [Send("sub_agent_subgraph", {
        "agent_id": f"sub_{state['domain']}_{t.id[:8]}",
        "parent_main_agent": state["agent_id"],
        "agent_type": "coding",
        "domain_snapshot": _domain_snapshot(state),
        "current_task": t,
        "sub_prompt": state["sub_agent_prompts"][t.id],
        "retry_count": 0,
    }) for t in state["scheduled_tasks"]]


def build_main_agent_subgraph(domain: str, client) -> CompiledGraph:
    agent = MainAgent(domain, client)
    g = StateGraph(MainAgentState)
    g.add_node("design", agent.design)
    g.add_node("schedule", agent.schedule)
    g.add_node("sub_agent_subgraph", build_sub_agent_subgraph(client))  # 嵌套子图
    g.add_node("aggregate", agent.aggregate)
    g.set_entry_point("design")
    g.add_edge("design", "schedule")
    g.add_conditional_edges("schedule", route_to_sub_agents)
    g.add_edge("sub_agent_subgraph", "aggregate")
    g.add_edge("aggregate", END)
    return g.compile()
```

#### Step 4: 子智能体子图（SDK 执行 + 验收 + 摘要，~5h）

> **v3.0 修正**：① 节点是普通 async 函数返回 state 增量（不要用 generator yield，事件靠 `adispatch_custom_event` 发）；② 流式沿用 **custom event**；③ 验收分层；④ 重试上限。
> **v4.0 增强**：① **L2 是唯一持有写工具的层**（OPT-1）；② 失败重试前 **`rewind_files()` 回滚污染**（OPT-3）；③ 子任务带**预算**，从 `ResultMessage.total_cost_usd` 累计真实成本（OPT-4）。

```python
SUB_AGENT_TOOLS = ["Read", "Write", "Edit", "Bash"]   # OPT-1：唯一有写权的层

class SubAgentNodes:
    """L2 执行单元。✅ 纯执行（唯一可写）。"""

    def __init__(self, client: ClaudeSDKClient):
        self.client = client                    # 长连接，供 rewind_files/get_context_usage
        self.runner = SubAgentRunner(client)
        self.verifier = LayeredVerifier()       # core/verify.py

    async def execute(self, state: SubAgentState) -> dict:
        # OPT-3：若是重试（retry_count>0），先回滚上次失败留下的文件污染
        if state["retry_count"] > 0 and state.get("last_msg_id"):
            await self.client.rewind_files(state["last_msg_id"])  # 需 enable_file_checkpointing

        # 冷启动或温启动（session 续接）
        if state.get("session_id"):
            result = await self.runner.warm_start(
                state["session_id"], state["current_task"])
        else:
            result = await self.runner.cold_start(
                state["current_task"], state["domain_snapshot"])
        # execute 内部已通过 adispatch_custom_event 发射 agent_text/agent_tool 事件
        return {"session_id": result.session_id, "status": "active",
                "last_msg_id": result.last_user_msg_id,        # OPT-3：记回滚锚点
                "cost_usd": result.total_cost_usd}             # OPT-4：真实成本（reducer 累加）

    async def verify(self, state: SubAgentState) -> dict:
        # 先确定性检查，红了或无法判定再上 LLM
        result = await self.verifier.run(
            domain=state["domain_snapshot"].domain,
            task=state["current_task"],
            criteria=state["sub_prompt"],
            project_dir=state["domain_snapshot"].project_dir,
        )
        return {"verify_passed": result.passed,
                "retry_count": state["retry_count"] + (0 if result.passed else 1)}

    async def summarize(self, state: SubAgentState) -> dict:
        summary = await self._gen_summary(state)   # 修改文件/新增接口/关键决策
        return {"sub_summaries": [summary],        # reducer 聚合回 L1
                "completed_tasks": [TaskRecord(...)],
                "status": "completed"}

    async def escalate(self, state: SubAgentState) -> dict:
        return {"status": "escalated",
                "sub_summaries": [f"[升级] {state['current_task'].id} "
                                  f"重试{state['retry_count']}次未过，需主智能体介入"]}


def route_after_verify(state: SubAgentState) -> str:
    if state["verify_passed"]:
        return "summarize"
    if state["retry_count"] >= 3:
        return "escalate"
    return "execute"


def build_sub_agent_subgraph(client) -> CompiledGraph:
    nodes = SubAgentNodes(client)
    g = StateGraph(SubAgentState)
    g.add_node("execute", nodes.execute)
    g.add_node("verify", nodes.verify)
    g.add_node("summarize", nodes.summarize)
    g.add_node("escalate", nodes.escalate)
    g.set_entry_point("execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", route_after_verify)
    g.add_edge("summarize", END)
    g.add_edge("escalate", END)
    return g.compile()
```

#### Step 5: 挂起池 + SDK Session 续接（~3h，P2 可后置）

```python
class AgentPool:
    """挂起池：记录 sub_agent → SDK session_id，挂起/温启动/销毁。

    ⚠️ 不再用 LangGraph Checkpointer 当 KV 存任意对象（误用）。
    存储底座 = SDK session_store + 轻量 SQLite 索引表。
    """

    def __init__(self, session_store: SessionStore, db_path: str):
        self.store = session_store
        self.db = sqlite3.connect(db_path)   # 表: (sub_agent_id, session_id, domain, status, last_active)

    async def suspend(self, sub_agent_id: str, session_id: str, domain: str):
        # SDK 已持久化会话本体到 session_store；此处只记索引
        self.db.execute("INSERT OR REPLACE INTO pool VALUES (?,?,?,?,?)",
                        (sub_agent_id, session_id, domain, "suspended",
                         datetime.now().isoformat()))
        self.db.commit()

    async def find_warm_candidate(self, domain: str) -> str | None:
        row = self.db.execute(
            "SELECT session_id FROM pool WHERE domain=? AND status='suspended' "
            "ORDER BY last_active DESC LIMIT 1", (domain,)).fetchone()
        return row[0] if row else None

    async def kill(self, sub_agent_id: str):
        row = self.db.execute("SELECT session_id FROM pool WHERE sub_agent_id=?",
                              (sub_agent_id,)).fetchone()
        if row:
            await delete_session_via_store(self.store, row[0])  # SDK 清理会话
        self.db.execute("DELETE FROM pool WHERE sub_agent_id=?", (sub_agent_id,))
        self.db.commit()
```

#### Step 6: SDK 原生压缩 + PreCompact + 用量监控兜底（~3h，P2 可后置）

> **v4.0 修正**：`get_context_usage()` 是 `ClaudeSDKClient` **实例方法**（须持长连接）；字段是 `percentage`（百分比，0–100），无 `.ratio`。主策略信任 SDK 自动压缩，KILL 仅兜底。

```python
class ContextGuard:
    """用 SDK 真实用量驱动，而非压缩不回喂的 state['messages']。"""

    async def assess(self, client: ClaudeSDKClient) -> str:
        usage = await client.get_context_usage()        # 实例方法，须长连接
        # 信任 SDK 自动压缩；仅当其被禁用且逼近上限时才 KILL 兜底
        if usage["percentage"] > 90 and not usage["isAutoCompactEnabled"]:
            return "kill_and_respawn"   # 摘要交接 → 新 session 冷启动
        return "continue"

# 原生压缩：注册 PreCompact 钩子，在 SDK 压缩前注入领域记忆，防关键上下文被压没
async def on_pre_compact(inp: PreCompactHookInput, tool_use_id, ctx: HookContext):
    # inp["trigger"]: "auto"|"manual"；可借 additionalContext 注入领域契约/关键决策
    return {"hookSpecificOutput": {"hookEventName": "PreCompact",
                                   "additionalContext": domain_memory_digest()}}

PRECOMPACT_HOOKS = {"PreCompact": [HookMatcher(hooks=[on_pre_compact])]}
```

#### Step 7: 分层验收器（~3h）

```python
class LayeredVerifier:
    """① 确定性检查（零LLM） → ② LLM 评审（兜底）"""

    async def run(self, domain, task, criteria, project_dir) -> VerifyResult:
        checks = DOMAIN_CHECKS.get(domain, [])      # 来自 domains.yaml
        deterministic = await self._run_commands(checks, project_dir)
        if deterministic.all_green:
            return VerifyResult(passed=True, detail=deterministic.report)
        # 红了或无确定性检查 → LLM 兜底
        return await self._llm_review(task, criteria, deterministic.report)

    async def _run_commands(self, checks, cwd) -> DeterministicResult:
        # checks 例: ["pytest -q", "ruff check .", "mypy ."] / ["npm test", "tsc --noEmit"]
        ...
```

#### Step 8: 构建星射线主图（子图嵌套 + 反向连线，~6h）

> **v3.0 修正全部图错误**：路由函数直接返回 Send（无 mapping dict、无 `state["sends"]`）；子图作为节点；补 finalize 收口与回流；异步 checkpointer。

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver   # ← 异步图用 aio 版

async def build_star_ray_graph(client) -> CompiledGraph:
    g = StateGraph(OrchestratorState)

    architect = ArchitectAgent(client)
    g.add_node("architect", architect.__call__)
    g.add_node("finalize", architect.finalize)

    # L1 子图作为节点（领域来自配置注册表，动态构建）
    g.add_node("main_agent_subgraph", build_main_agent_subgraph_dispatcher(client))

    # 快车道：退化为 Phase 1 单 Agent
    g.add_node("fast_lane", build_fast_lane_node(client))

    g.set_entry_point("architect")

    # L0 → L1（按 DAG 分批）或 → finalize 或 → fast_lane
    g.add_conditional_edges("architect", route_from_architect)

    # 回流：L1 子图完成 → 回 architect 再判断（DAG 是否还有下一批）
    g.add_edge("main_agent_subgraph", "architect")

    g.add_edge("fast_lane", END)
    g.add_edge("finalize", END)

    # 异步持久化（支持会话恢复 / 中断续跑）
    async with AsyncSqliteSaver.from_conn_string("checkpoints.db") as cp:
        return g.compile(checkpointer=cp)
```

> **回流环路说明**：`main_agent_subgraph → architect` 形成受控环。`architect` 每轮用 `dag.ready_batch(done=completed_domains)` 判断：还有就绪批次就再 Send 一批；没有就走 `finalize`。`completed_domains` 经 reducer 累加，保证环可收敛。需设 `recursion_limit` 足够覆盖 DAG 层数。

#### Step 9: ChatCLI 底层切换（~3h）

> **v3.0 修正**：流式沿用 `on_custom_event`（Phase 1 已验证），**不监听 `on_chat_model_stream`**（模型调用在 SDK 内部，LangGraph 收不到）。`--continue/--resume` 映射到图的 `thread_id`。

```python
class ChatCLI:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.graph = None                  # 异步构建，在 start() 里 await
        self.renderer = ChatRenderer()     # 复用 Phase 1

    async def _handle_message(self, prompt: str):
        initial = {"user_request": prompt, "project_dir": self.project_dir}
        config = {"configurable": {"thread_id": self.session_id},
                  "recursion_limit": 50}
        async for event in self.graph.astream_events(initial, config, version="v2"):
            # 只关心自定义事件（agent_text/agent_tool/agent_result/agent_error）
            if event["event"] == "on_custom_event":
                self.renderer.handle(self._to_chat_event(event))
            # 节点边界用于显示层级进度
            elif event["event"] == "on_chain_start":
                self._show_node_progress(event["name"])

    def _show_node_progress(self, name: str):
        label = {"architect": "👑 架构师分析中...",
                 "main_agent_subgraph": "🛡️ 主智能体规划中...",
                 "sub_agent_subgraph": "⚔️ 子智能体执行中...",
                 "finalize": "📝 汇总结果..."}.get(name)
        if label:
            self.renderer.handle(ChatEvent(type="TEXT", content=label))

    def _to_chat_event(self, ev: dict) -> ChatEvent:
        name, data = ev["name"], ev["data"]
        if name == "agent_text":   return ChatEvent(type="TEXT", content=data["text"])
        if name == "agent_tool":   return ChatEvent(type="TOOL_USE", tool_name=data["tool_name"])
        if name == "agent_result": return ChatEvent(type="TOOL_RESULT", result=data["content"])
        if name == "agent_error":  return ChatEvent(type="ERROR", content=data["error"])
        return ChatEvent(type="TEXT", content="")
```

#### Step 10: 测试与验证（~5h）

| 测试项 | 说明 |
|-------|------|
| **State reducer** | 并行写 `domain_reports` 等字段不报 `InvalidUpdateError` |
| **Send 路由** | 路由函数返回 `list[Send]`，按 DAG 分批正确 |
| **子图嵌套** | 主图 / MainAgent / SubAgent 三层子图可独立编译与组合 |
| **依赖 DAG** | 下游领域等上游契约就绪才触发 |
| **复杂度快车道** | 单领域小任务退化为单 Agent，跳过三层 |
| **反向瀑布流** | summary→aggregate→finalize 连线，最终答复非空 |
| **重试上限** | verify 连续失败 3 次 → escalate，不撞 recursion_limit |
| **确定性验收** | 测试/lint 全绿不调 LLM；红了走 LLM 兜底 |
| **工具层硬约束（OPT-1）** | L0/L1 调用 `Write`/`Edit`/`Bash` 应被 SDK 拒绝（不在 allowed_tools） |
| **结构化输出（OPT-2）** | Architect 返回 `structured_output` dict，无需文本解析 |
| **干净重试（OPT-3，P2）** | 重试前 `rewind_files` 回滚，失败代码不叠加 |
| **成本观测（OPT-4）** | `cost_usd` 经 reducer 累计，报告含真实 `$` |
| **温启动（P2）** | resume session 后模型"记得"上次改动 |
| **用量监控（P2）** | `get_context_usage().percentage` 逼近上限触发 KILL 重建 |
| **流式一致** | on_custom_event 正确渲染三层输出 |
| **会话恢复** | `--continue/--resume` 经 thread_id 正确恢复（区分 SDK session_id） |

---

## 6. Phase 2 验收标准（修正版）

| # | 验收项 | 通过条件 |
|---|--------|---------|
| 1 | **三层子图** | Architect/MainAgent/SubAgent 三层子图可独立编译并嵌套组合 |
| 2 | **脑手解耦（硬约束）** | L0/L1 的 `allowed_tools` 仅只读集，调用写工具被 SDK 拒绝；L2 纯执行（OPT-1 可机器验证） |
| 3 | **依赖感知路由** | 按 DAG 分批 Send，下游等上游契约就绪 |
| 4 | **并行无冲突** | reducer 正确聚合并行写，无 `InvalidUpdateError` |
| 5 | **双向瀑布流** | 向下只读快照 + 向上摘要回流，finalize 产出统一答复 |
| 6 | **复杂度快车道** | 简单需求退化单 Agent，不强行三层 |
| 7 | **重试可控** | verify 失败有上限，超限 escalate 不死循环 |
| 8 | **分层验收** | 确定性检查优先，LLM 兜底 |
| 9 | **结构化输出（OPT-2）** | Architect/验收用 `output_format`，免文本解析 |
| 10 | **成本可控可观测（OPT-4）** | `task_budget` 生效，报告含真实 `total_cost_usd` |
| 11 | **温启动（P2）** | resume SDK session，模型保留上轮记忆 |
| 12 | **上下文管理（P2）** | SDK 原生自动压缩 + PreCompact；`percentage` 逼近上限才 KILL |
| 13 | **CLI 入口不变** | `harness chat` 体验与 Phase 1 一致 |
| 14 | **流式一致** | on_custom_event 正确渲染三层 |
| 15 | **会话恢复** | `--continue/--resume` 不回退；thread_id 与 session_id 不混淆（OPT-7） |
| 16 | **测试通过** | 所有新增测试通过 |

---

## 7. 时间预估（修正版）

> **v3.0 修正**：原 37h 偏乐观。分布式 agent 基建（子图 + reducer + Send + session 续接 + 用量监控 + 流式打通 + 测试）按经验需上调。
> **v4.0 增量**：OPT-1（`allowed_tools`）、OPT-2（`output_format`）、OPT-7（概念厘清）改动极小、并入对应 Step 内，几乎不加工时；OPT-3/4/6 单列。下表区分 P0/P1/P2。

| Step | 内容 | 优先级 | 预估 |
|------|------|-------|-----:|
| Step 1 | 三层 State + 快照 + DAG（含 cost/last_msg_id 字段） | P0 | ~3h |
| Step 2 | 架构师 + 复杂度 + DAG + 收口（含 OPT-1/2） | P0 | ~5h |
| Step 3 | 主智能体子图（参数化，含 OPT-1） | P0 | ~7h |
| Step 4 | 子智能体子图 + 验收 + 重试（含 OPT-4 预算观测） | P0 | ~6h |
| Step 7 | 分层验收器 | P1 | ~3h |
| Step 8 | 星射线主图（子图嵌套 + 回流） | P0 | ~6h |
| Step 9 | ChatCLI 切换（custom event，含 OPT-7 厘清） | P0 | ~3h |
| Step 10 | 测试与验证 | P0 | ~5h |
| | **P0+P1 主干小计** | | **~38h** |
| Step 5 | 挂起池 + session 续接（含 OPT-3 rewind） | P2 | ~4h |
| Step 6 | SDK 原生压缩 + PreCompact + 用量监控兜底 | P2 | ~3h |
| OPT-6 | `can_use_tool` 危险护栏 | P2 | ~2h |
| | **含 P2 全量合计** | | **~47h（含联调缓冲建议预留 1.5×，约 9–12 工作日）** |

> **建议节奏**：先交付 P0 主干 + P0 级 OPT（OPT-1/2/7 随主干一起，几乎零成本却显著提升正确性）；再迭代 P1（验收、OPT-4/5）、P2（续命/压缩/OPT-3/6）。不要一次性全做。

---

## 8. 风险与应对（修正版）

| # | 风险 | 影响 | 应对 |
|---|------|------|------|
| 1 | 架构师拆解/DAG 不准 | 路由错误、下游空转 | 结构化输出校验 + 人工确认 + DAG 环检测 |
| 2 | **并行写 state 竞态** | `InvalidUpdateError`/覆盖 | **所有并行字段强制 reducer**（必修） |
| 3 | **test 回环无限重试** | 撞 recursion_limit | **retry_count 上限 + escalate**（必修） |
| 4 | **压缩 state 无效** | 误以为省了 token | 改用 SDK 真实用量 + 原生压缩（已重定义） |
| 5 | **温启动失忆** | query 无记忆 | 改用 SDK resume/session_id（已重定义） |
| 6 | 子图嵌套调试复杂 | 定位困难 | 每层子图独立单测 + 节点级 custom event 追踪 |
| 7 | 回流环不收敛 | 死循环 | `completed_domains` reducer 累加 + DAG 终止条件 |
| 8 | LLM 调用过多 | 延迟/成本 | 拆解与提示词合并为一次结构化输出 + 确定性验收 |
| 9 | 跨领域契约不一致 | 集成失败 | 接口契约 schema 校验纳入确定性验收 |
| 10 | SDK 版本 API 变动 | 续接/压缩失效 | 锁定 `claude-agent-sdk==0.2.93`，能力封装在适配层 |

---

## 9. 关键设计决策记录（v3.0 新增 ADR）

| ID | 决策 | 理由 |
|----|------|------|
| ADR-1 | 三层用 **subgraph 嵌套**而非扁平多 State | LangGraph 一图一 schema；嵌套即"分形"本义 |
| ADR-2 | 执行/会话/压缩**下沉 SDK 原生能力** | v0.2.93 已提供 agents/resume/fork/usage/precompact，避免重造 |
| ADR-3 | 领域**参数化 + 配置注册表** | 兑现"加领域零改编排代码" |
| ADR-4 | 验收**确定性优先、LLM 兜底** | 降幻觉、降成本，且为 Phase 3 Hooks 预留 |
| ADR-5 | 路由按 **依赖 DAG 分批** | 防下游基于不存在契约编码 |
| ADR-6 | 流式坚持 **custom event** | 模型调用在 SDK 内部，LangGraph 收不到 model 事件 |
| ADR-7 | 简单需求走**快车道** | 避免小任务付三层开销 |

---

## 10. 总结

Phase 1 已完成单 Agent MVP（SDK 连通、流式、CLI/REPL、TUI、会话恢复）。

Phase 2 采用**星射线三层分形架构**，v3.0 相比初版的核心修正：

| 维度 | 初版问题 | v3.0 修正 |
|------|---------|----------|
| **图结构** | 三 State 塞一张扁平图（无法编译） | subgraph 嵌套，一层一 schema |
| **路由** | Send 经 state 中转 + mapping dict 误用 | 路由函数直接返回 `list[Send]` |
| **并行** | 并行写无 reducer（必崩） | 所有并行字段强制 reducer |
| **瀑布流** | 反向上报是孤儿方法，图无连线 | aggregate/finalize 连线 + 收口答复 |
| **压缩** | 压 `state["messages"]`（不回喂，无效） | SDK 真实用量 + PreCompact 原生压缩 |
| **温启动** | query 无记忆，无法续命 | SDK resume/session_id 续接 |
| **领域扩展** | 三个硬编码文件（违背零修改） | 参数化 + 配置注册表 |
| **执行层** | 自建 subagent 循环 | 复用 SDK 原生 agents |
| **健壮性** | 无重试上限/无快车道/无 DAG | retry 上限 + 快车道 + 依赖 DAG |
| **流式** | 监听 model 事件（收不到） | 坚持 custom event |

Phase 2 完成后，系统实现：
- **降低上下文雪崩**：主脑只看摘要，执行层用 SDK 真实用量驱动压缩。
- **降低幻觉的局部专注**：子Agent 专属 System Prompt + 只读快照，不被全量代码库干扰。
- **架构的极致简洁**：新增领域仅改配置，编排层代码零修改。
- **经验的渐进沉淀**：SDK session 续接 + 挂起池，为 Phase 4 经验库铺路。

为 Phase 3（Hooks + Sandbox）和 Phase 4（经验沉淀）打下坚实基础。
