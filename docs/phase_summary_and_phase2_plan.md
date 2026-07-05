# Harness Agent — 项目阶段总结与 Phase 2 编排计划

> **架构代号**: Star-Ray (星射线)
> **当前阶段**: Phase 1 已完成，准备进入 Phase 2
>
> **权威设计文档**：Phase 2 架构以 [`docs/架构师 agent.md`](./架构师%20agent.md) 为准，本文档为编排计划对齐。
>
> **相关文档**：
> - [`架构师 agent.md`](./架构师%20agent.md) — **Phase 2 L0 Router 权威设计**
> - [`memory_system_design.md`](./memory_system_design.md) — 星型记忆拓扑 + GCH 设计（Phase 3+ 复用）
> - [`星射线 agent 编排.md`](./星射线%20agent%20编排.md) — 顶层愿景文档
> - [`critical_review.md`](./critical_review.md) — Red Team 评审备忘

---

## 1. 项目概述

**Harness Agent** 是一个基于 `claude-agent-sdk` 的智能开发编排系统，采用**L0 极薄 Router** 星射线架构，解决以下核心痛点：

| # | 痛点 | 星射线解决方案 | 目标阶段 |
|---|------|---------------|---------|
| 1 | 单领域 vs 多领域需求缺乏路由 | L0 Router：一次 LLM 调用判定，Fast Lane 自执行 / Full 模式派发 claude code 子进程 | Phase 2 |
| 2 | 跨领域并行执行工作区污染 | git worktree 物理隔离，每个领域独立工作区 | Phase 2 |
| 3 | 多领域并发编排与收口 | asyncio.gather 并行拉起 CLI 子进程，merge 回主分支 | Phase 2 |
| 4 | 状态管理混乱 | SDK session 续接 + 极简元信息（不维护状态机） | Phase 2 |
| 5 | 缺少代码审查 | Hooks（PreToolUse）自动拦截 | Phase 3 |
| 6 | 上下文爆炸 / 跨域记忆 | GCH 全局上下文中枢（星型记忆拓扑） | Phase 3+ |
| 7 | 经验无法沉淀 | 挂起机制 + .skills 知识库 | Phase 4 |

---

## 2. 项目路线图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Harness Agent 开发路线图 (星射线架构)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 1 ✅         Phase 2 🎯          Phase 3           Phase 4          │
│  ─────────         ─────────           ─────────         ─────────         │
│  单 Agent MVP      L0 Router            Hooks + 沙箱      经验沉淀          │
│                    (Fast Lane + Full)    代码质量保障      (.skills)         │
│                                                                             │
│  ▼                 ▼                    ▼                ▼                  │
│                                                                             │
│  SDK 连通          👑 L0 Router         PreToolUse Hook   SkillsAgent       │
│  流式渲染          LLM 路由判定         代码审查拦截      错误捕获 → 生成     │
│  CLI 流式渲染      Fast Lane 自执行      Sandbox 沙箱      .md 文件           │
│  Chat REPL         Full 模式 CLI 子进程  GCH 上下文中枢    知识库检索         │
│  TUI 全屏界面      git worktree 隔离     自建 L1/L2        注入 Agent        │
│                    动态权限硬约束         分层验收器                          │
│                    merge 收口                                                │
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
| **CLI 单次执行** | ✅ | `harness run "任务"` 支持流式渲染 |
| **Chat REPL** | ✅ | `harness chat` 多轮对话，支持历史会话恢复 |
| **TUI 全屏界面** | ✅ | 基于 `prompt_toolkit` 的全屏终端应用 |
| **流式渲染** | ✅ | 基于 `adispatch_custom_event` 的自定义事件流 |
| **斜杠命令** | ✅ | `/help`, `/exit`, `/clear`, `/stats`, `/context`, `/model`, `/undo` |
| **会话恢复** | ✅ | `--continue` 恢复最近会话，`--resume <id>` 恢复指定会话 |
| **取消请求** | ✅ | ESC 键取消当前请求 |
| **上下文接口** | ✅ | `ContextProvider` 协议定义，支持 Claude Code preset |

### 3.2 关键现状约束（Phase 2 设计的事实前提）

> ⚠️ 来自 Phase 1 代码的现状，直接决定 Phase 2 哪些机制可行：

1. **真正的对话历史在 SDK 内部。** SDK 自己跑 ReAct 循环，外部无法直接操作其上下文。
   → **推论**：压缩/续命必须作用于 **SDK 真实 session**。

2. **流式渲染走 `adispatch_custom_event`**，不是 LangChain model 事件。
   → **推论**：Phase 2 沿用 custom event。

3. **`query()` 是一次性调用**，无跨调用记忆；持久会话需改用 `ClaudeSDKClient` 或 `resume`/`session_id`。

4. **Phase 1 的 ChatSession（纯 SDK，不经过 LangGraph）是实际跑多轮对话的路径**，证明了纯 SDK 路线完全可行。

5. **路由判定必须走独立 stateless `query()`，不进常驻 session 历史。** 避免路由 JSON 污染后续 Fast Lane 执行流、避免 text 重复进入 session（详见 [`架构师 agent.md §3`](./架构师%20agent.md)）。

### 3.3 SDK 原生能力盘点（v0.2.93，必须优先复用）

> 经实测，`claude-agent-sdk` v0.2.93 已原生提供以下能力，Phase 2 **复用而非重造**：

| 能力 | SDK 入口 | 在星射线中的用途 |
|------|---------|-----------------|
| **独立 stateless query** | `query(prompt, options)` | 路由判定调用（不进常驻 session），Full 模式暂不直接使用（派发 CLI 子进程） |
| **Session 续接** | `resume` / `session_id` / `continue_conversation` | L0 Router 常驻 session 续接对话历史 |
| **上下文用量** | `ContextUsageResponse` / `ContextUsageCategory` | 用 SDK 真实 token 计数，Phase 3+ 驱动压缩决策 |
| **原生压缩** | `PreCompactHookInput` / `fold_session_summary` | Phase 3+ 在 SDK 即将压缩时介入 |
| **预算控制** | `task_budget` / `max_budget_usd` / `TaskBudget` | Fast Lane 执行成本上限（Phase 2 P2 可选） |
| **动态权限** | `can_use_tool` 回调 → `PermissionResult` | **Phase 2 P0**：按 lane 拦截业务写（Fast 放行 / Full 拒绝），硬约束核心机制 |
| **Session Store** | `session_store` / `SessionStore` | Phase 3+ 挂起池的存储底座 |
| **原生 Subagent** | `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` | Phase 3+ 自建 L1/L2 时复用 |
| **文件检查点** | `enable_file_checkpointing` | Phase 3+ |
| **Session Fork** | `fork_session` / `ForkSessionResult` | Phase 3+ |
| **Subagent 钩子** | `SubagentStartHookInput` / `SubagentStopHookInput` | Phase 3+ |

**设计原则**：Phase 2 L0 Router 聚焦核心路由与执行派发——充分利用 `query()`（独立 stateless 调用）和 `can_use_tool`（动态权限硬约束）。SDK 的 Subagent/续接/压缩等能力将在 Phase 3+ 自建 L1/L2 层级时发挥完整作用。

---

## 4. Phase 2 目标：L0 Router 编排系统

### 4.1 核心架构：L0 极薄 Router + Fast Lane / Full 模式

```
CLI 会话开始
  │
  ▼
L0Router 初始化（常驻 session，全工具集 + can_use_tool 硬约束）
  │
  ▼ 用户输入到达
  │
  _route(text)  ← 一次 LLM 调用，output_format 判定
  │
  ├── lane == "fast"（单领域）
  │     │
  │     ├→ _current_lane = "fast"
  │     ├→ 当前 session 直接执行（带写权限）
  │     └→ 流式返回，改完即止
  │
  └── lane == "full"（多领域）
        │
        ├→ _current_lane = "full"   ← 硬约束生效：L0 调业务写工具会被拒
        ├→ 每个 task → git worktree add
        ├→ asyncio.gather(claude -p 子进程 × N)   ← 各 worktree 为 cwd
        │     └─ claude code 内部自管 subagent，L0 不干预
        ├→ 各 worktree merge 回主分支
        └→ 收口汇总给用户
```

> ⚠ 路由判定(`_route`)异常时降级 Fast Lane(见 `架构师 agent.md §5`),不阻断用户；worktree 分支名带 uuid 短 id,防同领域重复执行冲突。

### 4.2 组件职责

| 组件 | 职责 | 限制 |
|-----|------|------|
| **L0 Router** (`core/architect.py`) | 路由判定（一次 LLM 调用判定单/多领域）、Fast Lane 自执行（带写权限）、Full 模式派发 claude code CLI 子进程、worktree 管理、merge 收口 | Full 模式下禁业务写入（`can_use_tool` 硬约束）；放行 Bash（git 运维必需） |
| **claude code CLI**（Full 模式） | 每个领域一个独立子进程，在各自 git worktree 下执行。内部自管 subagent 机制 | L0 不干预其执行细节，只下发 prompt + 收结果 |
| **常驻 session**（L0） | 与 CLI 会话同生命周期，持有 `ClaudeSDKClient`。Fast Lane 的执行历史自然沉淀于此 | 路由判定走独立 stateless `query()`，不污染 session 历史 |

### 4.3 核心机制

#### 4.3.1 路由判定：独立 stateless LLM 调用

```python
ROUTER_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["fast", "full"]},
        "reason": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "domain": {"type": "string"},
                "prompt": {"type": "string"},
                "intended_files": {"type": "array", "items": {"type": "string"}},
            }, "required": ["domain", "prompt"]},
        },
    },
    "required": ["lane", "reason", "tasks"],
}
```

路由判定用 `output_format` 输出结构化 `{lane, reason, tasks[]}`，**不进常驻 session 历史**——避免路由 JSON 污染后续执行流、避免 text 重复出现。详见 [`架构师 agent.md §3`](./架构师%20agent.md)。

#### 4.3.2 Fast Lane：L0 当前 session 直执行

单领域需求：L0 在常驻 session 直接执行，全工具已开（写权限放行）。text **首次**进入常驻 session，无重复、无路由 JSON 残留。自然完成对话。

#### 4.3.3 Full 模式：claude code CLI 子进程 + worktree

多领域需求：
1. 拆出各领域独立 prompt（自包含：需求 + 边界 + 验收）
2. 每个领域创建独立 git worktree（分支名 `l0-{domain}-{uuid6}` 防冲突）
3. `asyncio.gather` 并发拉起 `claude -p` 子进程，各自 worktree 为 cwd
4. 子进程内部用 claude code 的 subagent 机制，L0 不干预
5. 各 worktree merge 回主分支，冲突标记需人工介入
6. 清理 worktree（无论成功/异常均清理防残留）

#### 4.3.4 动态权限硬约束（can_use_tool）

L0 session 配全工具集，但 `can_use_tool` 准入回调按当前 lane 拒绝越界工具：

```python
BUSINESS_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

async def _lane_guard(self, input, ctx) -> PermissionResult:
    """fast 全放行；full/None 禁业务写，放行 Bash（git 运维必需）"""
    if self._current_lane != "fast" and input.tool_name in BUSINESS_WRITE_TOOLS:
        return PermissionResult(behavior="deny", message="...")
    return PermissionResult(behavior="allow")
```

**状态残留兜底**：入口 `handle_user_input` 将 `_current_lane` 重置为 `None`；回调对 `None` 按最严（等同 full）处理。

#### 4.3.5 路由降级

路由判定使用独立 `query()` 顶层 API（不经过 BaseAgentSession），异常（schema 不符/网络/解析失败）默认降级 Fast Lane 执行，不阻断用户。Fast 是安全兜底；确为多领域时用户重新发送即可重新判定。不搞交互式选择菜单。

### 4.4 Phase 2 不做的事（明确排除）

| 排除项 | 原因 |
|-------|------|
| ❌ 自建 L1（Main Agent）和 L2（Sub Agent）层 | Full 模式直接交给 claude code 的 subagent 机制，不自建。移至 Phase 3+ |
| ❌ GCH 全局上下文中枢（SQLite 存储 + MCP 工具表面） | Phase 2 先跑通 Router 骨架；单领域需求不需要。移至 Phase 3 |
| ❌ 分层验收器（确定性检查 + LLM 兜底） | 子进程内部自验证。移至 Phase 3+ |
| ❌ 依赖 DAG 调度 / TaskScheduler | Full 模式各领域 worktree 物理隔离，无跨需求依赖。Phase 3+ 需要时再加 |
| ❌ 挂起池 / 温启动（SDK session 管理） | 先冷启动跑通。移至 Phase 3 |
| ❌ Hooks 代码审查拦截 | Phase 3 |
| ❌ Sandbox 沙箱隔离 | Phase 3 |
| ❌ Skills 经验沉淀 | Phase 4 |
| ❌ Web/Flutter 前端 | Phase 5 |
| ❌ 脑手解耦完整形态（L0 只读） | Fast Lane 要 L0 自执行，必须带写权限。Phase 3 Full 模式可收紧 |
| ❌ GlobalDAG / 跨需求依赖合并 | Fast 同步执行、Full 各领域独立 worktree，无跨需求依赖 |
| ❌ 长轮询 / _completed_queue | Full 用 `asyncio.gather` 同步等子进程完成，不需要 |
| ❌ 同域收敛 / merge 冲突自动解决 | worktree 物理隔离已足够。冲突标记需人工介入 |
| ❌ 上下文压缩 / 用量监控兜底 | Fast Lane 短任务暂不触发。Phase 3+ 观察后再加 |
| ❌ 手动 `count_tokens` 估算窗口 | 不准，改用 SDK `get_context_usage()`（Phase 3+ 观察后再加） |

### 4.5 优先级排期表

| 机制 | 优先级 | 来源 | 说明 |
|------|-------|------|------|
| **L0 Router 核心骨架**（路由判定 + Fast Lane + Full 模式） | **P0 必做** | 架构师.md | 没有它整个架构不成立 |
| **LLM 路由判定 + `output_format`** | **P0 必做** | 架构师.md §3 | 一次调用输出 `{lane, reason, tasks[]}`，免文本解析 |
| **动态权限硬约束（`can_use_tool`）** | **P0 必做** | 架构师.md §4 | Full 模式禁业务写，Fast 放行；None 按最严兜底 |
| **Fast Lane 当前 session 直执行** | **P0 必做** | 架构师.md §5 | 复用常驻 session，全工具放行 |
| **Full 模式 CLI 子进程并发（`asyncio.gather`）** | **P0 必做** | 架构师.md §6 | 多领域场景的并发执行 |
| **git worktree 隔离** | **P0 必做** | 架构师.md §6.3 | 子进程之间互不污染 |
| **worktree merge 收口 + 清理** | **P0 必做** | 架构师.md §6.3 | 成功/异常均清理防残留 |
| **路由降级（异常→Fast Lane）** | **P0 必做** | 架构师.md §5/决策10 | 路由判定失败不阻断用户 |
| **worktree 分支名唯一标识** | **P0 必做** | 架构师.md §6.1/决策11 | `l0-{domain}-{uuid6}` 防同领域重复执行冲突 |
| **Bash 逃逸路径风险接受** | **P1 认知** | 架构师.md §4.2 | worktree 隔离兜底 Phase 2 接受；Phase 3 加 Bash 前缀白名单 |
| **Fast Lane 执行预算控制** | **P2 可后置** | — | `task_budget`/`max_budget_usd` 防失控 |
| **上下文压缩 / PreCompact** | **Phase 3+** | 后置 | Fast Lane 短任务暂不触发 |
| **GCH 全局上下文中枢** | **Phase 3+** | 后置 | 出现真实跨域契约协调需求再加 |
| **自建 L1/L2 / 分层验收 / 依赖 DAG** | **Phase 3+** | 后置 | 三层分形架构的完整主体 |
| **挂起池 / 温启动** | **Phase 3+** | 后置 | 需要跨会话记忆续接时再加 |
| **merge 冲突自动解决** | **Phase 3+** | 后置 | worktree merge 频繁冲突时再加 |
| **1M 上下文 beta** | **Phase 3+** | 后置 | 仅大仓库且成本可接受时开 |

### 4.6 机制 → SDK API 映射对照表

| 星射线机制 | 落地 SDK 能力 | 具体 API / 字段 | 对应 Step |
|-----------|--------------|----------------|----------|
| 路由判定（独立 stateless） | 独立 `query()` + `output_format`（不经过 BaseAgentSession） | `output_format` → `ResultMessage.structured_output` | Step 2 |
| Fast Lane 自执行 | 常驻 `ClaudeSDKClient.query()` | `ClaudeSDKClient` + `allowed_tools` 全开 | Step 3 |
| Full 模式派发 | `asyncio.create_subprocess_exec` | `claude -p ...` CLI 调用 | Step 4 |
| 动态权限硬约束 | 工具准入回调 | `can_use_tool` → `PermissionResult` | Step 2 |
| 流式渲染 | 内容块 → 自定义事件 | `AssistantMessage.content` blocks + `adispatch_custom_event` | Step 6 |
| 异常兜底 | SDK 异常 | `CLINotFoundError` / `CLIConnectionError` 等 | 全局 |

---

## 5. Phase 2 详细计划

> **关键变更**：Phase 2 实施范围从「三层分形架构全栈」缩减为 **「L0 极薄 Router」**。以下模块**不再属于 Phase 2**，已移至后置清单：
> - ~~`core/main_agent.py`~~、~~`core/sub_agent.py`~~（不自建 L1/L2，Full 模式用 claude code CLI）
> - ~~`core/gch/`~~、~~`core/verify.py`~~、~~`core/pool.py`~~、~~`core/guard.py`~~、~~`core/domains.yaml`~~、~~`core/schemas.py`~~（Phase 3+）
> - ~~`core/dag.py`~~ / ~~`TaskScheduler`~~（Full 模式无跨需求依赖）
>
> Phase 2 模块清单见下。

### 5.1 模块变更清单

> **修正**：基于 ADR-13（抽公共底座 + 双会话组合）更新模块清单，明确每个文件的改造量级。经代码审计发现 `chat/session.py` 需要大改而非微调，新增 `core/base_session.py` 底座文件。

| 文件路径 | 变更类型 | 改造量 | 说明 |
|---------|---------|:-----:|------|
| `core/base_session.py` | **新增** | 中 | **公共会话底座** — 从 `ChatSession` 抽离纯会话管理能力：SDK client 生命周期、自动重试/stderr 监控、取消信号处理、`can_use_tool` 权限回调挂载、session_store 持久化与续接。不含路由/命令/渲染逻辑。L0Router 内部常驻执行会话 + 路由临时会话均基于此接口 |
| `core/architect.py` | **新增** | 全新 | **L0 Router** — 路由判定（独立 `query()` + `output_format`，不经过 BaseAgentSession）+ Fast Lane 自执行 + Full 模式派发 + worktree 管理和收口。核心逻辑约 260 行。持有常驻 `BaseAgentSession`（Fast Lane 积累对话历史）；路由临时会话用独立 `query()` 顶层 API，用完即弃 |
| `core/worktree.py` | **新增** | 小 | git worktree 工具函数：`create_worktree()`、`merge_worktree()`、`remove_worktree()`。分支名 `l0-{domain}-{uuid6}` 防冲突 |
| `core/utils.py` | **新增** | 小 | 辅助函数：`_build_message()` — 统一封装消息格式对接 REPL 渲染事件，全项目复用 |
| `core/cli_utils.py` | **新增** | 小 | CLI 子进程工具函数：`_extract_result_text()`、`_extract_usage()` — 从 claude code headless 输出解析结果，与 Step 0 Spike 输出严格对齐，配套单测 |
| `chat/session.py` | **大改（瘦身）** | **大** | 继承 `BaseAgentSession`，仅保留 REPL 命令处理、渲染事件派发、斜杠命令逻辑。**Fast Lane 不复用完整 ChatSession**，L0Router 直接操作底座实例 |
| `chat/repl.py` | **中改（入口切换）** | 中 | 增加模式开关（单 Agent / L0 Router），默认走 L0 模式。斜杠命令 `/fast`、`/full` 强制指定车道跳过自动判定；`/clear` 清空常驻会话历史；`/undo` 仅 Fast Lane 生效 |
| `core/__init__.py` | **修改** | 小 | 导出 `L0Router`、`BaseAgentSession` |
| `pyproject.toml` | **修改** | 小 | 移除 `langgraph` 和 `langchain-core` 依赖（如未移除）；`uuid` 为 Python 标准库无需额外依赖 |
| `core/orchestrator.py` | **保留（无关联）** | 0 | Phase 1 遗留，`L0Router` 不经过，单 Agent 模式下仍可使用。Phase 2 结束后评估是否删除 |
| `core/state.py` | **已废弃，待删除** | 0 | Phase 2 收尾时清理。Phase 1 状态机逻辑已下线，仅留 docstring |
| ~~`core/main_agent.py`~~ | **后置 Phase 3+** | — | 不自建 L1 |
| ~~`core/sub_agent.py`~~ | **后置 Phase 3+** | — | 不自建 L2 |
| ~~`core/gch/`~~ | **后置 Phase 3+** | — | GCH 全局上下文中枢 |
| ~~`core/verify.py`~~ | **后置 Phase 3+** | — | 分层验收器 |
| ~~`core/pool.py`~~ | **后置 Phase 3+** | — | 挂起池 |
| ~~`core/guard.py`~~ | **后置 Phase 3+** | — | 危险工具护栏 |
| ~~`core/domains.yaml`~~ | **后置 Phase 3+** | — | 领域注册表（Router 的 `tasks` 从 LLM 输出动态获得） |
| ~~`core/schemas.py`~~ | **后置 Phase 3+** | — | 结构化 schema（纳入 `architect.py` 或后置） |
| ~~`core/dag.py`~~ | **后置 Phase 3+** | — | 依赖 DAG（无需跨需求依赖） |

#### 5.1.1 辅助函数归属

| 函数 | 文件 | 用途 |
|------|------|------|
| `_build_message()` | `core/utils.py` | 统一封装消息格式，对接 REPL 渲染事件，全项目复用 |
| `_extract_result_text()` / `_extract_usage()` / `_extract_cost()` | `core/cli_utils.py` | 从 claude code headless 输出解析结果文本 + token 用量 + 费用，与 Step 0 Spike 输出严格对齐，配套单测 |

#### 5.1.2 Session 持久化规则

| 会话类型 | 持久化策略 | 会话恢复支持 |
|---------|-----------|------------|
| **常驻执行会话**（Fast Lane） | 复用底座 `session_store` 能力，支持 `/continue` 续接 | ✅ 与单 Agent 体验一致 |
| **路由临时会话**（`_route()`） | 不持久化，用完即弃 | ❌ 无状态，每次新建 |
| **Full 模式子进程**（`_spawn_cli()`） | 一次性执行，不持久会话 | ❌ 执行完回收资源 |

#### 5.1.3 斜杠命令兼容性表

| 命令 | 兼容性 | 处理规则 |
|------|-------|---------|
| `/undo` | **仅 Fast Lane 生效** | Full 模式下禁用，提示「多领域模式不支持 undo，请人工处理」 |
| `/clear` | 全模式生效 | 清空常驻执行会话历史，重置 `_current_lane` 为 `None` |
| `/context` | 全模式生效 | 展示常驻会话的上下文用量 |
| `/fast` | **新增** | 强制当前输入走 Fast Lane，绕过自动判定 |
| `/full` | **新增** | 强制当前输入走 Full 模式，绕过自动判定 |
| `/help`、`/exit`、`/stats`、`/model` | 全模式生效 | 行为不变 |

> **P0: SLASH_PRE_INTERCEPT** — 所有 `/` 开头的命令在 `handle_user_input` 入口处前置拦截（`text.startswith('/')` 检查），不经过 `_route()` LLM 路由判定。
> 原因：斜杠命令语义与领域路由无关，走路由判定是无效的 LLM token 消耗。前置拦截直接委托常驻 session 处理，零开销。|

#### 5.1.4 `spawn_claude_code` 工具取舍

**结论：Phase 2 直接移除，不加入工具集。**

理由：Full 模式直接通过 `asyncio.create_subprocess_exec` 拉起 CLI 子进程，不走 SDK 的 `spawn_claude_code` 工具。后者是会话内派生，和我们「worktree 物理隔离、进程级隔离」的设计目标不符，可控性更差。Phase 3 如需会话内轻量派生再加回来。

#### 5.1.5 测试方案修正

| 测试项 | 修正后方案 | 备注 |
|-------|----------|------|
| Full 模式子进程 | Mock `asyncio.create_subprocess_exec`，模拟不同返回码和输出 | 不依赖真实 claude 命令，单测可稳定复现 |
| worktree 隔离 | 用临时目录初始化空 git 仓库，跑完整创建-合并-清理流程 | CI 配置 git user 信息即可，无权限问题 |
| 动态权限硬约束 | 先通过 Spike 核实 SDK 回调签名，再用 Mock SDK 验证回调逻辑 | 不依赖真实模型调用 |
| CLI 子进程契约 | 加一个标记为 `@pytest.mark.skip_ci` 的集成测试，本地手动验证 | CI 不执行，避免依赖外部环境 |

### 5.2 任务分解

> 执行顺序调整。新增 **Step 0 CLI Spike** 消灭 Full 模式最大不确定性；**Step 1 抽 BaseAgentSession 底座**最大化复用现有代码。所有 Step 的代码示例与 [`docs/架构师 agent.md`](./架构师%20agent.md) 不一致时以架构师 agent.md 为准。

#### Step 0: Claude CLI 契约 Spike（~0.5 天）

> **前置必做**。在写 `_spawn_cli` 之前先消灭最大不确定性。验证项全部有结论再写业务代码。

| 验证项 | 验证目的 | 输出标准 |
|-------|---------|---------|
| **命令行参数名核实** | 确认 flag 真实名称 | 明确 `-p`/`--prompt`、`--allowed-tools`/`--allowedTools`、`--output-format`、`--cwd` 的正确写法 |
| **stream-json 输出结构** | 确定结果解析逻辑 | 抓取完整输出样例，定位最终结果文本、token 用量、退出状态的字段路径 |
| **退出码规则** | 异常判断依据 | 正常完成 = 0，错误 = 非 0，确认超时/中断场景的退出码 |
| **cwd 下配置加载行为** | 确认隔离有效性 | 子进程是否会读取 worktree 下的 `.claude/` 配置？是否会继承全局配置？会不会污染主项目 |
| **工具限制生效性** | 确认权限围栏有效 | `--allowed-tools` 是否真的能禁用 Write/Bash 等工具 |

**Spike 输出物：**
- 文档附录新增「Claude Code CLI Headless 契约表」，标注版本号
- 确定 `_extract_result_text()`、`_extract_usage()` 的实现逻辑（写入 `core/cli_utils.py`）
- 输出可直接运行的最小验证命令，后续集成测试复用

#### Step 1: 抽 BaseAgentSession 底座 + 改造 ChatSession（~1 天）

> **重要**：这是 ADR-13（抽底座 C 方案）的落地。从 `chat/session.py` 的 570 行代码中抽离纯会话管理能力，L0Router 组合两类会话实例。

```python
# core/base_session.py — 公共会话底座

class BaseAgentSession:
    """纯执行底座，不涉及路由/命令/渲染逻辑。

    能力清单：
    - SDK client 生命周期管理 (start/close)
    - 自动重试、异常兜底、stderr 监控
    - 取消信号处理、检查点机制
    - session_store 持久化与续接 (resume/continue)
    - can_use_tool 权限回调挂载
    - 上下文用量查询 (get_context_usage)
    """

    def __init__(
        self,
        project_dir: str,
        model: str | None = None,
        system_prompt: str = "",
        allowed_tools: list[str] | None = None,
        session_store: SessionStore | None = None,
        can_use_tool: Callable | None = None,
        enable_checkpointing: bool = False,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        continue_conversation: bool = False,
    ) -> None: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, prompt: str) -> AsyncIterator[ChatEvent]: ...
    async def query(self, prompt: str) -> AsyncIterator[Message]: ...
    def cancel(self) -> None:
    async def recover_after_cancel(self) -> None:
    async def hot_restart(self) -> None:
```

现有 `ChatSession` 瘦身为继承 `BaseAgentSession`，仅保留 REPL 命令处理、渲染事件派发、斜杠命令逻辑，保留给单 Agent 模式使用。

#### Step 2: L0 Router 核心 — 路由判定（~1 天）

> **对齐**：`_route()` 改为轻量级 `query()` 顶层 API（不新建 BaseAgentSession）；`handle_user_input` 返回 `AsyncIterator[ChatEvent]` 流。

```python
# core/architect.py — 路由判定部分 + 底座组合
#
# 关键设计：
# - _route() 使用独立 query() 顶层 API,不经过 BaseAgentSession
# - 路由判定输出 ROUTER_DECISION_SCHEMA 定义的结构化结果
# - handle_user_input 返回 ChatEvent 流,而非 Message 列表
# - Fast Lane 通过 self._session.send(text) 产出 ChatEvent

from harness_agent.core.base_session import BaseAgentSession
from harness_agent.core.cli_utils import _extract_result_text, _extract_usage, _extract_cost

ROUTER_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["fast", "full"]},
        "reason": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "domain": {"type": "string"},
                "prompt": {"type": "string"},
                "intended_files": {"type": "array", "items": {"type": "string"}},
            }, "required": ["domain", "prompt"]},
        },
    },
    "required": ["lane", "reason", "tasks"],
}

ROUTE_JUDGE_PROMPT = """判断用户需求属于单领域还是多领域执行。
- 单领域 → lane="fast"
- 多领域 → lane="full"
返回 JSON: {"lane": "fast"|"full", "reason": "...", "tasks": [{"domain": "...", "prompt": "..."}]}"""

EXECUTOR_PROMPT = """你是 Harness Agent 的执行者。
当前处于 {lane} 模式。
- Fast Lane: 直接在常驻会话中执行
- Full 模式: …（动态 Prompt 增强）"""


class L0Router:
    """L0 极薄 Router。组合常驻 BaseAgentSession + 独立路由判定。"""

    def __init__(self, project_dir: str, model: str | None = None):
        self._current_lane: str | None = None
        self.project_dir = project_dir
        self.model = model
        self._total_cost: float = 0.0
        self._worktree_registry: dict[str, str] = {}
        self.CLI_TIMEOUT: int = 900  # P0: ASYNCIO_TIMEOUT

        # ── 常驻执行会话（Fast Lane 积累对话历史）──
        self._session = BaseAgentSession(
            project_dir=project_dir,
            model=model,
            system_prompt=EXECUTOR_PROMPT,
            allowed_tools=[
                "Read", "Glob", "Grep",
                "Write", "Edit", "NotebookEdit",
                "Bash",
            ],
            can_use_tool=self._lane_guard,
            session_store=create_session_store(),
        )

    async def start(self) -> None:
        await self._session.start()

    async def _route(self, text: str) -> dict:
        """独立 stateless 路由判定 —— 轻量 query() 顶层 API，不新建 BaseAgentSession。"""
        from claude_agent_sdk import query, ClaudeAgentOptions

        opts = ClaudeAgentOptions(
            system_prompt=ROUTE_JUDGE_PROMPT,
            allowed_tools=[],  # 路由判定纯 LLM 判定，不需要工具
            output_format={"type": "json_schema", "schema": ROUTER_DECISION_SCHEMA},
            model=self.model,
        )
        async for msg in query(prompt=text, options=opts):
            if hasattr(msg, 'structured_output') and msg.structured_output:
                return msg.structured_output
        raise RuntimeError("路由判定未返回结构化结果")

    async def handle_user_input(self, text: str) -> AsyncIterator[ChatEvent]:
        # P0: SLASH_PRE_INTERCEPT — 斜杠命令前置拦截，跳过路由判定
        #     /clear, /context, /undo, /help 等语义与领域路由无关，
        #     直接交给常驻 session 处理，避免无效消耗路由 LLM token
        if text.startswith('/'):
            self._current_lane = None
            async for event in self._session.send(text):
                yield event
            return

        # 1. 入口重置 lane (防异常残留)
        self._current_lane = None

        # 2. 路由判定 — 轻量 query(),不进常驻会话
        try:
            decision = await self._route(text)
        except Exception as e:
            yield ChatEvent("text", "⚠️ 路由判定异常，已降级单领域执行。")
            self._current_lane = "fast"
            async for event in self._session.send(text):
                yield event
            return

        lane = decision.get("lane", "fast")
        self._current_lane = lane

        yield ChatEvent("text", f"🔀 路由判定: {lane} ({decision.get('reason', '')})")

        if lane == "fast":
            # Fast Lane — 常驻会话自执行
            async for event in self._session.send(text):
                yield event

            # 累积费用
            if self._session.last_result and self._session.last_result.total_cost_usd:
                self._total_cost += self._session.last_result.total_cost_usd
        else:
            # Full 模式 — CLI 子进程 + worktree (Step 4 实现)
            async for event in self._run_full(text, decision):
                yield event
```

> **对齐**：
> 1. `_route()` 用 `claude_agent_sdk.query()` 顶层 API + `output_format`，不再新建 BaseAgentSession
> 2. `handle_user_input` 返回 `AsyncIterator[ChatEvent]`（而非 `Message`）
> 3. Fast Lane 通过 `self._session.send(text)` 产出 ChatEvent 流
> 4. 路由降级路径直接 `self._session.send(text)`，保持 ChatEvent 流一致

#### Step 3: Fast Lane 对接常驻会话 + 动态权限回调（~0.5 天）

因为底座已包含 can_use_tool 挂载能力，Step 3 只需确认 `_lane_guard` 回调正确挂载到常驻会话，Fast Lane 流式链路跑通。预计 0.5 天。

#### Step 4: Full 模式 — CLI 子进程 + worktree 隔离 + merge 收口（~1 天）

移除 `--verbose`（污染 stream-json）；增加 `_extract_cost()` 解析费用；`_spawn_cli()` 明确 `--cwd` 传递方式，不使用 `create_subprocess_exec(cwd=...)`。

```python
# core/cli_utils.py — 从 stream-json 输出解析结果

def _extract_result_text(stdout: str) -> str:
    """从 claude code stream-json 输出提取最终结果文本。
    与 Step 0 Spike 输出严格对齐。
    """
    ...

def _extract_usage(stdout: str) -> dict | None:
    """从 stream-json 输出提取 token 用量。
    返回 {"input": ..., "output": ...} 或 None。
    """
    ...

def _extract_cost(stdout: str) -> float | None:
    """从 stream-json 输出提取费用（USD）。
    由 ResultMessage.total_cost_usd 字段解析。
    """
    ...
```

#### Step 5: REPL 集成 + 斜杠命令适配（~0.5 天）

ChatCLI 入口增加模式开关（默认走 L0）。新增 `/fast`/`/full` 命令。`/undo` 仅 Fast Lane 生效。`/clear` 重置 `_current_lane` 为 `None`。

#### Step 6: 测试补全 + 废弃代码清理（~1 天）

全部 P0 项测试策略按 §5.1.5「测试方案修正」执行。mock 策略降低 CI 依赖。清理 `core/state.py` 废弃 docstring。

#### 执行顺序依赖图

```
Step 0: CLI Spike (0.5天)
   ↓
Step 1: BaseAgentSession 底座 (1天)
   ↓
Step 2: L0 Router 骨架 + 路由判定 (1天)
   ↓
Step 3: Fast Lane 对接 (0.5天)
   ↓
Step 4: Full 模式 CLI 子进程 (1天)
   ↓
Step 5: REPL 集成 + 命令 (0.5天)
   ↓
Step 6: 测试 + 清理 (1天)
```

总墙钟时间约 **5.5 天**，所有风险点全部前置验证。

#### Step 5: ChatCLI 集成 L0Router（~2h）

```python
# chat/repl.py — 集成 L0Router

class ChatCLI:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.router = L0Router(project_dir)        # Phase 2 核心
        self.renderer = ChatRenderer()

    async def _handle_message(self, prompt: str):
        # Phase 2: 经 L0Router 处理 (路由 + Fast Lane / Full)
        async for message in self.router.handle_user_input(prompt):
            self.renderer.handle(message)
```

Fast Lane 下 `L0Router` 的常驻 session 自然积累对话历史，下一次输入可继续对话。Full 模式下各子进程独立执行，L0 session 只做派发和汇总，不积累领域执行细节。

#### Step 6: 测试与验证（~3h）

| 测试项 | 说明 |
|-------|------|
| **LLM 路由判定** | 单领域需求返回 `{lane: "fast"}`，多领域返回 `{lane: "full", tasks: [...]}` |
| **路由降级** | 路由判定异常（网络/schema 不符）→ 降级 Fast Lane 不阻断用户 |
| **Fast Lane 自执行** | 单领域需求由当前 session 正常执行，不改文件时不涉及写工具；`can_use_tool` 全放行 |
| **Full 模式并发** | 多领域需求拆出 N 份 prompt，并发拉起 N 个子进程（mock claude CLI） |
| **worktree 隔离** | 每个子进程工作在独立 git worktree，互不干扰 |
| **worktree 清理** | 成功和异常分支的 worktree 均被清理，`.harness/worktrees/` 无残留 |
| **merge 收口** | 各 worktree 变更 merge 回主分支；冲突标记需人工介入 |
| **动态权限硬约束** | Fast Lane 下业务写工具放行；Full 模式下 `Write/Edit/NotebookEdit` 被 `can_use_tool` 拒绝；`Bash` 放行（git 运维）；`None`（异常残留）按 Full 最严处理 |
| **状态残留兜底** | 连续多次 `handle_user_input` 调用：每次 `_current_lane` 被入口重置为 `None`，不影响后续调用 |
| **CLI 子进程契约** | `claude -p` headless 调用成功执行并解析输出 |
| **流式一致** | Fast Lane 常规流式；Full 模式先 emit 一句"并行处理中…"再 await gather，最后 emit 收口汇总 |
| **无后置机制残留** | 代码中无 GCH 调用、无 `TaskState` 枚举、无 GlobalDAG / 长轮询引用 |

---

## 6. Phase 2 验收标准

验收项 15（BaseAgentSession 底座）+ 验收项 16（斜杠命令兼容）+ 验收项 17（spawn_claude_code 移除确认）+ 验收项 13 行数调整。

| # | 验收项 | 通过条件 |
|---|--------|---------|
| 1 | **LLM 路由判定** | 单领域需求返回 `{lane: "fast"}`，多领域返回 `{lane: "full", tasks: [...]}`；路由判定走独立 `query()`，不进常驻 session |
| 2 | **路由降级** | 路由判定异常（网络/schema 不符）→ 降级 Fast Lane 执行，不阻断用户 |
| 3 | **Fast Lane 自执行** | 单领域需求由 L0 常驻会话直接完成（带写权限），不拉外部进程 |
| 4 | **Full 模式并发** | 多领域需求拆出 N 份 prompt，并发拉起 N 个 claude code 子进程 |
| 5 | **worktree 隔离** | 每个 Full 子进程在独立 git worktree 执行，分支名 `l0-{domain}-{uuid6}` 带唯一标识，互不干扰 |
| 6 | **merge 收口** | 各 worktree 变更 merge 回主分支；成功/异常均清理 worktree 防残留 |
| 7 | **动态权限硬约束** | Full 模式下 `Write/Edit/NotebookEdit` 被 `can_use_tool` 拒绝；`Bash` 放行（运维）；Fast Lane 下全部放行；`None`（异常残留）按 Full 最严处理 |
| 8 | **CLI 子进程契约** | `claude -p` headless 调用成功执行并解析输出；与 Step 0 Spike 输出的契约表对齐 |
| 9 | **CLI 入口不变** | `harness chat` 体验与 Phase 1 一致（Fast Lane 保持对话连续性） |
| 10 | **流式一致** | Fast Lane 常规流式；Full 模式先 emit 进度后 await gather，最后 emit 汇总 |
| 11 | **无后置机制残留** | 代码中无 GCH 调用、无 `TaskState` 枚举、无 GlobalDAG / 长轮询 / Analyst 分裂引用 |
| 12 | **无 LangGraph 依赖** | `pyproject.toml` 中不含 langgraph/langchain-core；代码中无 StateGraph/Send/reducer 引用 |
| 13 | **极薄 Router** | `core/architect.py` 核心逻辑 ≤ ~260 行；不维护状态机 |
| 14 | **测试通过** | 所有新增测试通过 |
| 15 | **BaseAgentSession 底座** | 从 `chat/session.py` 成功抽离纯会话管理能力，L0Router 组合常驻 + 临时两类底座实例，`ChatSession` 瘦身继承底座 |
| 16 | **斜杠命令兼容** | `/undo` 仅 Fast Lane 生效；`/clear`/`/context` 全模式生效；`/fast`/`/full` 新增命令强制指定车道 |
| 17 | **spawn_claude_code 已移除** | L0Router 工具集中不含 `spawn_claude_code`；Full 模式走 `asyncio.create_subprocess_exec` |
| 18 | **子进程超时兜底（P0: ASYNCIO_TIMEOUT）** | `_spawn_cli` 调用 `asyncio.wait_for` 包裹 `proc.communicate()`，超时后 SIGTERM→SIGKILL 升级；`CLI_TIMEOUT` 可配置（默认 900s） |
| 19 | **worktree 创建回滚（P0: WORKTREE_ROLLBACK）** | `_run_full` 中 worktree 创建循环包裹在 try/except 中，失败时逆序清理已创建的 worktrees 再 re-raise |
| 20 | **斜杠命令前置拦截（P0: SLASH_PRE_INTERCEPT）** | `handle_user_input` 入口对 `text.startswith('/')` 直接委托常驻 session，不经过 `_route()` |
| 21 | **Full 结果持久化规则（P0: FULL_RESULT_PERSISTENCE）** | Full 模式仅最终 summary 写入 session 历史；中间日志显示但不持久化；Full 模式不调 `self._session.send()` |

---

## 7. 时间预估

| Step | 内容 | 优先级 | 预估 |
|------|------|-------|-----:|
| Step 0 | Claude CLI 契约 Spike（验证 flag 名/输出格式/退出码/配置加载） | P0 | ~0.5d |
| Step 1 | 抽 BaseAgentSession 底座 + 改造 ChatSession 瘦身 | P0 | ~1d |
| Step 2 | L0 Router 核心 — 路由判定 + 底座组合（常驻+临时） | P0 | ~1d |
| Step 3 | Fast Lane 对接常驻会话 + 动态权限回调挂载 | P0 | ~0.5d |
| Step 4 | Full 模式 — CLI 子进程 + worktree 隔离 + merge 收口 | P0 | ~1d |
| Step 5 | ChatCLI 集成 L0Router + 斜杠命令适配（`/fast`/`/full`） | P0 | ~0.5d |
| Step 6 | 测试补全（mock 策略）+ 废弃代码清理（`core/state.py`） | P0 | ~1d |
| | **P0 主干小计** | | **~5.5d** |

相比初始估算增加了 Step 0 Spike、底座抽离和废弃代码清理。
联调缓冲建议预留 1.2x，约 **6-7 工作日**。

### 7.1 后置模块（Phase 3+）时间参考

| 模块 | 触发条件 | 预估 | 落点 |
|------|---------|-----:|------|
| GCH 全局上下文中枢（SQLite + MCP 工具 + LLM 馆员） | 出现真实跨域契约协调需求 | ~10-15h | Phase 3 |
| 自建 L1/L2 层 + 分层验收 + 依赖 DAG | Full 模式子进程协作需要编排名义层 | ~15-20h | Phase 3 |
| merge 冲突自动解决 | worktree merge 频繁冲突 | ~3-5h | Phase 3 |
| 挂起池 / 温启动 | 需要跨会话记忆续接 | ~3-5h | Phase 3 |
| Hooks 代码审查拦截 / Sandbox | 代码质量保障需求 | ~8-12h | Phase 3 |
| Skills 经验沉淀 | 经验积累需求 | ~8-10h | Phase 4 |
| Web/Flutter 前端 | 可视化需求 | ~20-30h | Phase 5 |

---

## 8. 风险与应对

| # | 风险 | 影响 | 应对 |
|---|------|------|------|
| 1 | 路由判定误判（fast 误判 full 或反之） | 路由错误 | 判定带 `reason` 字段可审计；用户可即时纠正；宁可误 fast 也别漏（fast 开销小，降级策略见架构师.md §5） |
| 2 | Full 模式 LLM 越界写业务代码 | 业务代码被 L0 直接修改 | `can_use_tool` 硬约束 deny `Write/Edit/NotebookEdit`，不靠 prompt 自觉 |
| 3 | Full 模式 Bash 逃逸（经 bash 写文件绕过 Write） | 越界写风险 | Phase 2 接受：worktree 物理隔离兜底，L0 cwd 在主目录不污染子进程工作区；Phase 3 加 Bash 命令前缀白名单 |
| 4 | `_current_lane` 异常残留导致权限错乱 | 安全漏洞 | 入口 `handle_user_input` 重置为 None；回调对 None 按最严（禁业务写）兜底 |
| 5 | 多 worktree merge 冲突 | 部分领域无法自动合并 | Phase 2 标记"需人工介入"；Phase 3 加冲突解决子进程 |
| 6 | claude code CLI flag 随版本变动 | 子进程调用失败 | headless 调用封装在 `_spawn_cli`，单一适配点 |
| 7 | 子进程异常退出 | 部分领域失败 | `gather(return_exceptions=True)` 捕获，汇总报告标注 ❌；异常分支也清理 worktree 防残留 |
| 8 | 路由判定异常中断整个流程 | 用户被阻断 | `try/except` 捕获，降级 Fast Lane 执行；提示用户可重新发送重判 |
| 9 | 同领域重复执行 worktree 命名冲突 | 并发冲突 | 分支名带 uuid 短 id（`l0-{domain}-{uuid6}`），每次执行独立 |
| 10 | Fast Lane 长任务撑爆 L0 session 上下文 | 上下文溢出 | Phase 2 先观察；触发后置 Phase 3 的压缩兜底 |
| 11 | SDK 版本 API 变动 | `can_use_tool` 签名等变化 | 锁定 `claude-agent-sdk==0.2.93`，回调封装在 `L0Router` 内部 |
| 12 | **P0: `_spawn_cli` 子进程无超时 → 子进程僵尸挂死 L0** | L0 永久 hang，无法响应用户 | `asyncio.wait_for` 包裹 `proc.communicate()`（默认 900s），超时后 SIGTERM→SIGKILL 升级；配置暴露为 `CLI_TIMEOUT` 类常量 |
| 13 | **P0: worktree 创建循环部分失败 → 残留垃圾文件** | `.harness/worktrees/` 下残留孤立 worktree | worktree 循环包裹 `try/except`，异常时逆序清理已创建的 worktrees |
| 14 | **P0: 斜杠命令未前置拦截 → 无效消耗路由 LLM token** | `/clear`、`/context` 等命令支付不必要的模型调用成本 | `handle_user_input` 入口 `text.startswith('/')` 检查，跳过 `_route()` |
| 15 | **P0: Full 模式结果写入 session 历史规则未定义 → 可能污染历史** | 中间日志持久化挤占上下文，或关键 summary 丢失 | 仅最终 summary 写入 assistant 消息；中间日志显示但不持久化；Full 模式不调 `self._session.send()` |

---

## 9. 关键设计决策记录

| ID | 决策 | 理由 |
|----|------|------|
| ADR-1 | ~~三层 subgraph 嵌套~~ → ~~纯 async 函数 + asyncio.gather~~ → **L0 极薄 Router** | 纯 async 方案已正确移除了 LangGraph，但三层分形架构（L0/L1/L2 全自建）对于 Phase 2 范围过大。当前方案将 L1/L2 后置到 Phase 3+，Full 模式改为 `claude code` CLI 子进程 + git worktree 物理隔离。 |
| ADR-2 | 执行/会话/压缩 **下沉 SDK 原生能力** | v0.2.93 已提供 `query`/`can_use_tool`/`resume`/`fork`/`usage`/`precompact`，避免重造 |
| ADR-3 | 路由判定用 **LLM（一次 `output_format` 调用）**，不用正则 | 正则太死板——可能本涉及一个领域却匹配到多领域关键词。模型能力足够直接决策。独立 stateless query 不进常驻 session |
| ADR-4 | Full 模式 **不自建 L1/L2**，改用 **claude code CLI 子进程 + git worktree** | claude code 自带完善的 subagent 识别 + 独立工作区机制，不重造。L0 只做"判领域 + 分 worktree + 派发 + 收结果 + merge" |
| ADR-5 | **worktree 物理隔离** | Full 模式多个子进程并行写同一项目目录必撞车。每个领域一个 git worktree，做完 merge 回主分支。git 原生，成本极低 |
| ADR-6 | 权限用 **`can_use_tool` 动态准入**，不用静态 `allowed_tools` | L0 Fast Lane 需要写权限（自执行），Full 模式 L0 只应派发。静态白名单无法同时满足两个场景，动态回调按 lane 来 |
| ADR-7 | 路由降级：**宁可误 fast，不可误 full** | 路由判定异常默认降级 Fast Lane 执行，不阻断用户。fast 是安全兜底；确为多领域时用户重新发送即可 |
| ADR-8 | **worktree 分支名带 uuid 短 id** | `l0-{domain}-{uuid6}`，防同领域重复执行时分支名/路径冲突（必现 bug），每次执行独立可追溯 |
| ADR-9 | **LLM 路由判定走独立 stateless `query()`** | 不进常驻 session 历史——避免路由 JSON 污染后续执行流、避免 text 重复 |
| ADR-10 | **只禁业务写（Write/Edit/NotebookEdit），放行 Bash** | Full 模式下 L0 仍需执行 git worktree/merge 等运维操作，一刀切禁 Bash 会让 Full 流程跑不通 |
| ADR-11 | 入口重置 `_current_lane = None` + 回调对 None 按最严兜底 | 防异常状态残留导致权限错乱；None 仅在异常残留时出现，按最严处理最安全 |
| ADR-12 | GCH / 自建 L1-L2 / 分层验收 / 依赖 DAG **全部后置 Phase 3+** | Phase 2 先跑通 Router 骨架。复杂协调机制等真正需要时再加。过早引入会拖慢交付 |

---

## 10. 总结

Phase 1 已完成单 Agent MVP（SDK 连通、流式、CLI/REPL、TUI、会话恢复）。

Phase 2 采用 **L0 极薄 Router 架构**，核心变更：

| 维度 | 原方案（三层分形全栈） | 当前方案（L0 极薄 Router） |
|------|---------------------|---------------------|
| **L0 职责** | 入口架构师：拆解 + DAG + 复杂度判定 + 收口 | 极薄 Router：一次 LLM 调用判 lane（Fast/Full），Fast 自执行 / Full 派发 CLI 子进程 |
| **L1/L2 实现** | 自建：MainAgent（参数化类）+ SubAgent（执行循环） | 不自建：Full 模式用 claude code CLI 子进程（自带 subagent 机制）。自建 L1/L2 后置 Phase 3+ |
| **GCH 全局记忆** | P0 必做：SQLite 事实源 + MCP 工具表面 | Phase 3+：出现真实跨域契约协调需求时再加 |
| **权限模型** | 静态 `allowed_tools`（L0/L1 只读，L2 可写） | 动态 `can_use_tool`（Fast 放行 / Full 禁业务写 / None 最严兜底） |
| **并行隔离** | git worktree（L2 级别） | git worktree（Full 模式领域级别），分支名 `l0-{domain}-{uuid6}` 带唯一标识 |
| **路由判定** | 一次 LLM `output_format` 输出 `{complexity, tasks, prompts, dag}` | 一次 LLM `output_format` 输出 `{lane, reason, tasks[]}`；独立 stateless `query()`，不进常驻 session |
| **验收** | 分层验收器（确定性 + LLM 兜底） | 子进程内部自验证。Phase 3+ 自建 L1/L2 时再加 |
| **依赖调度** | DAG + TaskScheduler（按依赖分批） | 无（Full 模式各领域独立 worktree，无跨需求依赖） |
| **快车道** | Fast Lane：单 Agent 执行 | Fast Lane：L0 当前常驻 session 直执行（带写权限，不拉外部进程） |
| **状态管理** | 数据类 + 函数参数 + SQLite 编排进度 | 极简元信息（`_current_lane`、`_worktree_registry`、`_total_cost`），不维护状态机 |
| **路由降级** | 未明确 | 异常默认降级 Fast Lane，不阻断用户 |
| **代码量** | ~300 行 async 函数 + 调度器 + ~8 个新模块 | `core/architect.py` ≤ ~250 行 + `core/worktree.py` ~80 行 |
| **P0 时间** | ~27h | ~16.5h |

Phase 2 完成后，系统实现：
- **极简路由**：一次 LLM 调用判定单/多领域，单领域自执行、多领域并发派发 claude code 子进程。
- **物理隔离**：git worktree 保证多领域并行执行互不污染。
- **动态权限**：`can_use_tool` 按 lane 硬约束业务写，Fast 放行、Full 拒绝、None 最严兜底。
- **架构简洁**：`core/architect.py` ≤ ~250 行，不建 L1/L2、不做状态机、零框架依赖。
- **渐进路线**：自建 L1/L2、GCH、分层验收等复杂能力后置 Phase 3+，先跑通 Router 骨架。

Phase 2 的 Router 骨架为 Phase 3（GCH + 自建 L1/L2 + Hooks + Sandbox）和 Phase 4（经验沉淀）提供坚实的路由基础。

> **权威设计文档**：Phase 2 架构细节以 [`docs/架构师 agent.md`](./架构师%20agent.md) 为准，本文档为编排计划对齐。
