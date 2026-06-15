# Harness Agent — 项目阶段总结与 Phase 2 编排计划

> **版本**: v1.0 | **日期**: 2026-06-14 | **当前阶段**: Phase 1 已完成，准备进入 Phase 2

---

## 1. 项目概述

**Harness Agent** 是一个基于 `claude-agent-sdk` 和 `LangGraph` 的智能开发编排系统，目标构建多 Agent 有状态协作系统，解决以下核心痛点：

| # | 痛点 | 解决方案 | 目标阶段 |
|---|------|---------|---------|
| 1 | 多终端上下文隔离 | 共享上下文管理器 | Phase 2 |
| 2 | 上下文传递成本高 | LangGraph State + 摘要注入 | Phase 2 |
| 3 | 缺少代码审查 | Hooks（PreToolUse）自动拦截 | Phase 3 |
| 4 | 经验无法沉淀 | .skills 知识库自动生成 | Phase 4 |

---

## 2. 项目路线图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Harness Agent 开发路线图                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 1 ✅         Phase 2 🎯          Phase 3           Phase 4          │
│  ─────────         ─────────           ─────────         ─────────         │
│  单 Agent MVP      Agent 编排          Hooks + 沙箱      经验沉淀           │
│                    (无状态系统)          代码质量保障      (.skills)          │
│                                                                             │
│  │                 │                    │                │                  │
│  │                 │                    │                │                  │
│  ▼                 ▼                    ▼                ▼                  │
│                                                                             │
│  SDK 连通          Router 节点         PreToolUse Hook   SkillsAgent       │
│  LangGraph 直线    多 Agent 节点       代码审查拦截      错误捕获 → 生成     │
│  CLI 流式渲染      上下文注入          Docker 沙箱       .md 文件           │
│  Chat REPL         状态流转            MCP Tools         知识库检索         │
│  TUI 全屏界面      意图分发            自动验证          注入 Agent         │
│                                                                             │
│                                                                             │
│  Phase 5 (前端)    Phase 6 (优化)                                                      │
│  ─────────────    ─────────────                                                       │
│  Web + Flutter     向量检索、性能优化                                                  │
│  可视化仪表盘      自适应策略                                                          │
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
| **流式渲染** | ✅ | 实时显示 Agent 文本、工具调用、执行结果 |
| **Spinner 动画** | ✅ | Agent 思考/工具执行时显示动态动画 |
| **斜杠命令** | ✅ | `/help`, `/exit`, `/clear`, `/stats`, `/context`, `/model`, `/undo` |
| **会话恢复** | ✅ | `--continue` 恢复最近会话，`--resume <id>` 恢复指定会话 |
| **取消请求** | ✅ | ESC 键取消当前请求 |
| **上下文接口** | ✅ | `ContextProvider` 协议定义，支持 Claude Code preset |

### 3.2 项目结构

```
src/harness_agent/
├── __init__.py                    # 版本号 (0.1.1)
├── cli.py                         # CLI 入口 + 命令定义
├── config.py                      # 配置管理
├── context/
│   ├── __init__.py
│   └── provider.py                # ContextProvider 协议（上下文可插拔接口）
├── core/
│   ├── __init__.py
│   ├── orchestrator.py            # LangGraph 编排引擎（直线图）
│   ├── state.py                   # HarnessState 状态定义
│   └── agents/
│       ├── __init__.py
│       └── base_agent.py          # Agent 基类（纯函数 + 事件发射）
└── chat/
    ├── __init__.py
    ├── events.py                  # ChatEvent 事件类型定义（跨 Phase 长期资产）
    ├── session.py                 # ChatSession（SDK 会话封装，过渡态）
    ├── renderer.py                # ChatRenderer（事件 → TUI 渲染）
    ├── repl.py                    # ChatCLI（REPL 主循环 + 会话管理）
    ├── tui_app.py                 # TuiApp（全屏 TUI 应用）
    ├── content_buffer.py          # ContentBuffer（内容缓冲区）
    ├── commands.py                # 斜杠命令注册与分发
    └── session_store.py           # 会话存储管理
```

### 3.3 关键设计决策

#### 3.3.1 "脑裂"设计

LangGraph State 和 SDK 内部状态是两个独立的世界：

- **LangGraph State**：仅作为执行结果的「归档记录」
- **SDK 内部状态**：由 `claude-agent-sdk` 内部的 ReAct 状态机闭环管理

Phase 2 的解决方案：通过「摘要注入 prompt」来桥接两个世界。

#### 3.3.2 ChatSession 过渡态标记

`ChatSession` 是 Phase 2.5 的单 Agent 封装，Phase 3 引入多 Agent 路由后：
- **退役**：多轮对话管理权交还给 LangGraph StateGraph + Checkpointer
- **复用**：`ChatEvent` 协议、`ChatRenderer`、`ContextProvider` 是长期资产

#### 3.3.3 事件驱动架构

`ChatEvent` 统一事件类型，连接 Session 层和 Renderer 层：

| 事件类型 | 说明 |
|---------|------|
| `TEXT` | 模型文本输出 |
| `TOOL_USE` | 工具调用开始 |
| `TOOL_RESULT` | 工具执行结果 |
| `TURN_START` | 一轮对话开始 |
| `TURN_END` | 一轮对话结束 |
| `CANCELLED` | 用户取消请求 |
| `THINKING` | Agent 思考状态 |
| `ERROR` | 错误 |
| `USAGE` | Token 用量统计 |

---

## 4. Phase 2 目标：Agent 编排（无状态系统）

### 4.1 核心目标

将单 Agent 升级为多 Agent 协作系统，通过 LangGraph StateGraph 实现 Agent 编排：

```
                     ┌──────────┐
                     │  START   │
                     └────┬─────┘
                          │
                          ▼
                 ┌────────────────┐
                 │  Router 节点    │ ← 分析用户意图，决定分发目标
                 │  (意图分类)     │
                 └───┬────┬────┬──┘
                     │    │    │
           ┌─────────┘    │    └─────────┐
           ▼              ▼              ▼
     ┌───────────┐  ┌───────────┐  ┌───────────┐
     │ Frontend  │  │   API     │  │  Backend  │
     │  Agent    │  │  Agent    │  │  Agent    │
     └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
           │              │              │
           └──────┬───────┴──────┬───────┘
                  │              │
                  ▼              ▼
           ┌────────────┐  ┌──────────┐
           │  Context   │  │  Direct  │
           │  摘要注入  │  │  Return  │
           └────┬───────┘  └─────┬────┘
                │                │
                ▼                ▼
          ┌──────────┐     ┌──────────┐
          │   END    │     │   END    │
          └──────────┘     └──────────┘
```

### 4.2 Phase 2 不做的事（明确排除）

| 排除项 | 原因 |
|-------|------|
| ❌ Docker 沙箱验证 | Phase 3 的工作 |
| ❌ Hooks 代码审查 | Phase 3 的工作 |
| ❌ Skills 经验沉淀 | Phase 4 的工作 |
| ❌ Web/Flutter 前端 | Phase 5 的工作 |
| ❌ 有状态持久化 | Phase 3 用 LangGraph Checkpointer 处理 |

---

## 5. Phase 2 详细计划

### 5.1 模块变更清单

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `core/state.py` | **扩展** | 增加 `shared_context`, `current_agent`, `agent_history` 字段 |
| `core/orchestrator.py` | **重构** | 从直线图升级为条件路由图 |
| `core/router.py` | **新增** | 意图分类器（分析用户输入，决定分发目标） |
| `core/agents/frontend_agent.py` | **新增** | 前端 UI 开发专家 |
| `core/agents/api_agent.py` | **新增** | 前后端接口联调专家 |
| `core/agents/backend_agent.py` | **新增** | 后端业务开发专家 |
| `core/agents/general_agent.py` | **新增** | 通用开发助手（处理无法明确分类的任务） |
| `core/context/shared_context.py` | **新增** | 三层共享上下文管理器 |
| `chat/repl.py` | **修改** | 保持 ChatCLI 入口不变，底层切到 LangGraph |
| `chat/session.py` | **退役** | 被 LangGraph Agent 节点替代 |

### 5.2 任务分解

#### Step 1: 扩展 HarnessState（~2h）

```python
class HarnessState(TypedDict):
    """Phase 2 状态定义"""

    # ── 归档记录（Phase 1 保留）──
    messages: Annotated[list[BaseMessage], add_messages]

    # ── Phase 2 新增 ──
    task: str                                    # 用户原始任务描述
    project_dir: str                             # 项目工作目录

    # Agent 编排状态
    current_agent: str                           # 当前执行的 Agent 名称
    agent_history: list[AgentExecutionRecord]    # Agent 执行历史（用于上下文桥接）
    retry_count: int                             # 重试计数（失败后重新分发）

    # 共享上下文（由 ContextProvider 管理）
    shared_context: dict                         # 项目结构、技术栈、规范等全局信息
    domain_context: dict                         # 前端/后端/接口领域上下文
```

#### Step 2: 实现三层共享上下文管理器（~3h）

```python
class SharedContext:
    """三层上下文管理"""

    # Level 1: 全局上下文 —— 所有 Agent 共享
    # 包含：项目结构、技术栈、开发规范、Git 状态
    global_context: GlobalContext

    # Level 2: 领域上下文 —— 相关 Agent 共享
    # 前端：组件树、路由结构、样式系统
    # 后端：API 定义、DB Schema、业务模型
    # 接口：API 契约、数据格式、认证方式
    domain_contexts: dict[str, DomainContext]

    # Level 3: 任务上下文 —— 单个 Agent 私有
    # 当前任务描述、进行中的修改、临时状态
    task_contexts: dict[str, TaskContext]
```

**上下文注入策略**：

```python
# 通过 ClaudeAgentOptions 的 system_prompt 注入
options = ClaudeAgentOptions(
    system_prompt=f"""你是{agent_role}。

## 项目全局信息
{shared_context.global_context.to_prompt()}

## 领域上下文
{shared_context.get_domain(agent_domain).to_prompt()}

## 当前任务
{task_description}
""",
    cwd=project_dir,
)
```

#### Step 3: 实现意图路由器（~4h）

```python
class IntentRouter:
    """意图分类器

    分析用户输入，决定分发到哪个 Agent。
    使用 Claude SDK 快速分类（单次 query，max_turns=1）。
    """

    async def classify(self, prompt: str) -> RouterDecision:
        """分析用户意图

        Returns:
            RouterDecision:
                - target_agent: "frontend" | "api" | "backend" | "general"
                - confidence: 0.0 ~ 1.0
                - reasoning: 分类理由（简短）
        """
        ...
```

**路由规则**：

| 关键词/模式 | 目标 Agent |
|------------|-----------|
| UI、组件、样式、React、Vue、前端 | `frontend_agent` |
| API、接口、联调、前后端、REST、GraphQL | `api_agent` |
| 后端、业务、数据库、服务、逻辑 | `backend_agent` |
| 其他/混合 | `general_agent`（默认） |

#### Step 4: 实现专业 Agent（~6h）

每个 Agent 都是基于 `BaseAgent` 的扩展，有特定的 system_prompt 和领域上下文：

```python
class FrontendAgent(BaseAgent):
    """前端 UI 开发专家"""

    def __init__(self, shared_context: SharedContext):
        super().__init__(
            name="frontend",
            system_prompt=self._build_frontend_prompt(),
            allowed_tools=["Read", "Write", "Edit", "Bash"],
        )
        self.domain = "frontend"

    def _build_frontend_prompt(self) -> str:
        return """你是前端 UI 开发专家。

## 专业能力
- React/Vue/Angular 组件开发
- CSS/SCSS/Tailwind 样式处理
- 状态管理（Redux/Vuex/Pinia）
- 前端工程化（Webpack/Vite）

## 工作原则
- 优先考虑用户体验和可维护性
- 保持组件单一职责
- 注重代码复用
"""
```

同理实现 `APIAgent`、`BackendAgent`、`GeneralAgent`。

#### Step 5: 重构 LangGraph 编排引擎（~6h）

```python
def build_multi_agent_graph() -> StateGraph:
    """Phase 2 多 Agent 状态图"""

    graph = StateGraph(HarnessState)

    # ── 节点 ──
    graph.add_node("router", intent_router_node)
    graph.add_node("frontend_agent", frontend_agent_node)
    graph.add_node("api_agent", api_agent_node)
    graph.add_node("backend_agent", backend_agent_node)
    graph.add_node("general_agent", general_agent_node)
    graph.add_node("context_inject", context_inject_node)

    # ── 入口 ──
    graph.set_entry_point("router")

    # ── 条件路由 ──
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "frontend": "frontend_agent",
            "api": "api_agent",
            "backend": "backend_agent",
            "general": "general_agent",
        }
    )

    # ── Agent 完成后 ──
    for agent in ["frontend_agent", "api_agent", "backend_agent", "general_agent"]:
        graph.add_edge(agent, "context_inject")

    graph.add_edge("context_inject", END)

    return graph
```

#### Step 6: ChatCLI 底层切换（~3h）

保持 `ChatCLI` 的 REPL 入口不变，但内部从 `ChatSession` 切换到 `LangGraph`：

```python
class ChatCLI:
    """REPL 主循环（Phase 2 版）"""

    def __init__(self, ...):
        # Phase 1: 使用 ChatSession
        # self.session = ChatSession(...)

        # Phase 2: 使用 LangGraph Orchestrator
        self.orchestrator = MultiAgentOrchestrator(...)
        self.graph = self.orchestrator.graph

    async def _handle_message(self, prompt: str):
        """发送消息 → LangGraph 流式执行"""
        initial_state = {
            "messages": [],
            "task": prompt,
            "project_dir": self.project_dir,
            ...
        }

        async for event in self.graph.astream_events(initial_state, version="v2"):
            # 复用 Phase 1 的 ChatRenderer 处理事件
            self.renderer.handle(event_from_langgraph(event))
```

#### Step 7: 上下文桥接机制（~4h）

解决"脑裂"问题：Agent 执行完成后，将结果摘要注入下一轮：

```python
async def context_inject_node(state: HarnessState) -> dict:
    """上下文注入节点

    在 Agent 完成后执行：
    1. 提取 Agent 执行结果摘要
    2. 更新 shared_context
    3. 为下一轮准备桥接信息
    """
    # 提取摘要
    last_message = state["messages"][-1]
    summary = extract_summary(last_message)

    # 记录执行历史
    record = AgentExecutionRecord(
        agent=state["current_agent"],
        task=state["task"],
        summary=summary,
        tool_calls=extract_tool_calls(last_message),
    )
    state["agent_history"].append(record)

    # 更新共享上下文（可选）
    update_shared_context(state["shared_context"], record)

    return {
        "agent_history": state["agent_history"],
        "shared_context": state["shared_context"],
    }
```

#### Step 8: 测试与验证（~4h）

| 测试项 | 说明 |
|-------|------|
| Router 分类准确性 | 测试不同类型的输入是否被正确分发 |
| Agent 上下文注入 | 验证 shared_context 是否正确注入 Agent |
| 多 Agent 协作 | 测试连续任务是否能在不同 Agent 间流转 |
| 上下文桥接 | 验证执行历史是否能正确传递给下一轮 |
| 事件流一致性 | 验证 ChatRenderer 能正确消费 LangGraph 事件 |

---

## 6. Phase 2 验收标准

| # | 验收项 | 通过条件 |
|---|--------|---------|
| 1 | **Router 节点** | 能根据用户意图正确分发到目标 Agent |
| 2 | **多 Agent 节点** | Frontend/API/Backend/General 四个 Agent 可独立执行 |
| 3 | **上下文注入** | Agent 执行时能获得正确的领域上下文 |
| 4 | **事件流一致** | ChatRenderer 能正确渲染多 Agent 的输出 |
| 5 | **CLI 入口不变** | `harness chat` 用户体验与 Phase 1 一致 |
| 6 | **状态流转** | Agent 执行结果能正确更新 State |
| 7 | **历史桥接** | 多轮对话能访问之前的执行历史 |
| 8 | **测试通过** | 所有新增测试通过 |

---

## 7. 时间预估

| Step | 内容 | 预估时间 |
|------|------|---------:|
| Step 1 | 扩展 HarnessState | ~2h |
| Step 2 | 三层共享上下文管理器 | ~3h |
| Step 3 | 意图路由器 | ~4h |
| Step 4 | 专业 Agent 实现 | ~6h |
| Step 5 | LangGraph 编排重构 | ~6h |
| Step 6 | ChatCLI 底层切换 | ~3h |
| Step 7 | 上下文桥接机制 | ~4h |
| Step 8 | 测试与验证 | ~4h |
| | **合计** | **~32h（约 4-5 个工作日）** |

---

## 8. 风险与应对

| # | 风险 | 影响 | 应对 |
|---|------|------|------|
| 1 | Router 分类不准确 | Agent 收到错误任务 | 使用 Claude SDK 分类 + 设置 fallback Agent |
| 2 | 上下文 token 过长 | Agent 失去关键信息 | 分层注入 + 摘要压缩 |
| 3 | LangGraph-SDK 脑裂未解决 | 多轮对话 Agent "失忆" | Step 7 的上下文桥接机制 |
| 4 | ChatSession 退役影响 | Phase 2.5 的会话恢复功能丢失 | Phase 3 用 LangGraph Checkpointer 恢复 |
| 5 | 多 Agent 并行复杂 | 开发成本高 | Phase 2 先做串行编排，Phase 3 再考虑并行 |

---

## 9. Phase 2 后的架构愿景

Phase 2 完成后，系统架构将升级为：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户交互层                                      │
│                        harness chat (CLI TUI)                                │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LangGraph 编排层                                    │
│                                                                              │
│                    ┌────────────────────────────┐                           │
│                    │      Router 节点            │                           │
│                    │    (意图分类 + 分发)         │                           │
│                    └─────────┬──────────────────┘                           │
│                              │                                               │
│            ┌─────────────────┼─────────────────┐                            │
│            │                 │                 │                            │
│            ▼                 ▼                 ▼                            │
│     ┌──────────┐      ┌──────────┐      ┌──────────┐                        │
│     │ Frontend │      │   API    │      │ Backend  │                        │
│     │  Agent   │      │  Agent   │      │  Agent   │                        │
│     └────┬─────┘      └────┬─────┘      └────┬─────┘                        │
│          │                 │                 │                              │
│          └─────────────────┼─────────────────┘                              │
│                            │                                                 │
│                            ▼                                                 │
│                    ┌────────────────────────────┐                           │
│                    │   SharedContext 管理器     │                           │
│                    │  (三层上下文 + 桥接注入)     │                           │
│                    └────────────────────────────┘                           │
│                                                                              │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          执行层                                              │
│                                                                              │
│                    ┌────────────────────────────┐                           │
│                    │    claude-agent-sdk         │                           │
│                    │  (query / ClaudeSDKClient)  │                           │
│                    └────────────────────────────┘                           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 10. 总结

Phase 1 已成功完成单 Agent MVP，实现了：
- SDK 连通与流式渲染
- CLI 单次执行与 Chat REPL
- TUI 全屏交互界面
- 会话恢复与取消请求

Phase 2 的核心任务是：
- **引入 Router 节点**：分析意图，智能分发
- **实现多 Agent**：Frontend/API/Backend/General 四个专业 Agent
- **构建共享上下文**：三层上下文管理器，解决多 Agent 间的信息共享
- **状态桥接机制**：通过摘要注入解决 LangGraph-SDK "脑裂"问题

Phase 2 完成后，系统将从单 Agent 升级为多 Agent 无状态协作系统，为后续 Phase 3（Hooks + 沙箱）和 Phase 4（经验沉淀）打下坚实基础。