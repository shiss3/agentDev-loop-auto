# Harness Agent — 记忆系统设计（星型记忆拓扑）

> **日期**: 2026-07-02 | **架构代号**: Star-Ray / Star-Memory
> **适用阶段**: Phase 3+ (Phase 2 仅 L0 Router，不涉及 GCH)
> **取代**: 原 `phase_summary_and_phase2_plan.md` §4.3.2「记忆瀑布流」、§4.3.3「对象池温启动」的记忆语义、§4.3.4「60% 阈值压缩»
> **SDK 底座**: `claude-agent-sdk==0.2.93`（详见 `.claude/CLAUDE.md`）
>
> **相关文档**：
> - [`架构师 agent.md`](./架构师%20agent.md) — Phase 2 架构决策：**GCH 已正式后置到 Phase 3+**，Phase 2 仅 L0 Router
> - [`星射线 agent 编排.md`](./星射线%20agent%20编排.md) — 顶层愿景文档（L0 Architect / L1 Main / L2 Sub 三层为**未来愿景**）
> - [`critical_review.md`](./critical_review.md) — Red Team 评审，部分批评已吸收

---

## 0. 一句话定位

废除「向下快照派发 + 向上摘要回流」的多层瀑布流，改为
**「各节点记忆完全自治 + 全局中枢单点收敛 + MCP 工具按需读写」** 的星型记忆拓扑。
从根源消除多层传递的信息损耗、链路冗余与架构复杂度。

> **Phase 2 状态**: GCH 已被 `架构师 agent.md` 正式后置到 **Phase 3+**。
> Phase 2 仅实现 L0 Router（极薄路由器），不自建三层编排，也不涉及跨域记忆/全局中枢。
> 本文档的设计在 Phase 3+ 引入 GCH 时仍完全适用，仅执行时间顺延。

---

## 1. 三条铁律

| # | 铁律 | 含义 | SDK 落点 |
|---|------|------|---------|
| 1 | **记忆绝对自治** | 每个 Agent = 一个独立 `claude-agent-sdk` session，各自完整持有自身对话历史与上下文。互不直接注入、互不交叉修改。压缩 / 续接 / 销毁完全由节点自身管理 | `ClaudeSDKClient` + `session_id` / `resume` / `fork_session` |
| 2 | **全局信息唯一收口** | 全项目级、跨领域的公共上下文，**只**收敛到 GCH 一个节点。不存在任何 Agent 之间的直接记忆传递 | 进程内 MCP Server（`create_sdk_mcp_server`） |
| 3 | **按需拉取，零冗余** | 系统不做被动上下文投喂。Agent 需要全局/跨域信息时，主动通过 MCP 工具向 GCH 查询；不需要则零加载 | `mcp_servers` + `allowed_tools` 能力裁剪 |

> **关键认知（来自 Phase 1 踩坑）**：模型真正看到的上下文在 **CLI 子进程 / SDK session 内部**，不在任何外部队列或变量里。
> 因此「记忆自治」不是一句口号 —— 它是顺着 SDK 边界设计的必然结果：跨 Agent 强行注入历史，等于和 SDK 的 ReAct 状态机打架。

---

## 2. 星型拓扑

> **未来愿景说明**：本节图示的 L0 Architect / L1 Main / L2 Sub 三层结构是**长期愿景**（见 `星射线 agent 编排.md`）。
> Phase 2 仅落地 L0 Router，且 Full 模式直接复用 claude code CLI 子进程 + git worktree（每个子进程独立 session 天然隔离，无需 GCH 协调）。
> 下图描述的是 GCH 真正落地时（Phase 3+）的记忆拓扑。

```
                          ┌───────────────────────────────┐
                          │        🧠 GCH                  │
                          │   Global Context Hub           │
                          │   (全局记忆总线 / 单点收口)     │
                          │                                │
                          │  • 存储优先：结构化事实源       │
                          │  • LLM 馆员：可选的模糊综合查询 │
                          │  • 进程内 MCP Server            │
                          │  • SQLite 持久化（跨会话存活）  │
                          └───┬────────────┬───────────┬───┘
                  写+读 (MCP) │            │ 写+读      │ 写+读
              ┌───────────────┘            │           └───────────────┐
              │                            │                           │
        ┌─────┴─────┐                ┌─────┴─────┐               (Architect 也读写)
        │ 👑 L0     │                │ 🛡️ L1     │
        │ Architect │                │ Main A    │  ... Main B / Main C 同理，各自直连 GCH
        └─────┬─────┘                └─────┬─────┘
              │ 仅派发任务                  │ 派发任务 + 回收报告
              │ (不传递记忆)                │ (不传递记忆)
              ▼                            ▼
        (L1 Main Agents)         ┌─────────┴─────────┐
                                 ▼                   ▼
                            ┌─────────┐         ┌─────────┐
                            │ ⚔️ L2   │         │ ⚔️ L2   │
                            │ Sub     │         │ Test    │
                            │ (编码)  │         │ (检测)  │
                            └────┬────┘         └────┬────┘
                                 │ 返回结构化报告     │ 返回 pass/fail
                                 └──────►父 L1◄───────┘
                          ❌ L2 完全不接触 GCH（无任何 GCH 工具）
```

**连线语义**
- 实线连 GCH 的只有 **L0 与 L1**：双向（写结构化摘要 + 按需查询）。
- L1 → L2 是**任务派发 + 报告回收**，走的是函数调用关系，**不是记忆传递**。
- L2 → 父 L1 是**返回值**（`@dataclass` 结构化数据），不是记忆注入；父 L1 决定是否蒸馏后写入 GCH。
- L2 与 GCH **零连接**。

---

## 3. 记忆放置：什么存在哪里

| 记忆种类 | 存放位置 | 拥有者 | 生命周期 | 是否进 GCH |
|---------|---------|--------|---------|-----------|
| 完整对话历史、工具调用链、局部推理 | **Agent 自身 SDK session** | 各 Agent | 节点自治（压缩/续接/销毁） | ❌ 否 |
| 领域工作上下文（设计草稿、临时笔记） | Agent 自身 session | L1/L2 | 节点自治 | ❌ 否 |
| 结构化运行摘要（任务完成情况） | **GCH** | L0 / L1 写入 | 项目级持久 | ✅ 是 |
| 跨域接口契约 / 接口签名 | **GCH** | L1 注册、Architect 仲裁 | 项目级持久 | ✅ 是 |
| 全局决策 / 技术选型 / 项目事实 | **GCH** | L0 / L1 | 项目级持久 | ✅ 是 |
| 任务台账（task ledger） | **GCH** | L0 / L1 | 项目级持久 | ✅ 是 |
| 子 Agent 执行结果报告 | 父 L1 的 session（临时） | L2 产出 → L1 接收 | 临时，用完即弃 | ⚠️ 仅父蒸馏后 |

> **核心原则**：GCH 存的是**结构化的耐久事实**，绝不是原始对话流水。原始流水永远留在各自 session 里自生自灭。

---

## 4. GCH（全局上下文中枢）设计

### 4.1 GCH 不是一个"全程 LLM Agent"，而是"存储优先 + 可选 LLM 馆员"

> ⚠️ **优化点（最大成本项）**：如果 GCH 每次读写都触发一次 ReAct 循环，成本和延迟会爆炸。
> 所以 GCH 的默认形态是**确定性存储服务**，只有"模糊/跨域综合"查询才落 LLM。

```
┌───────────────────────────────────────────────────────────────┐
│                          GCH                                  │
│                                                               │
│   ┌─────────────────────┐      ┌──────────────────────────┐  │
│   │  Structured Store    │      │   LLM Librarian (可选)    │  │
│   │  (事实源 of truth)   │◄─────┤   仅模糊/综合查询时调用    │  │
│   │                      │      │   query() max_turns=1     │  │
│   │  • SQLite 持久化      │      │   结果按 (查询,版本) 缓存  │  │
│   │  • 精确读写 = 0 token │      │                          │  │
│   └─────────────────────┘      └──────────────────────────┘  │
│            ▲                                                  │
│            │ MCP 工具调用 (进程内, create_sdk_mcp_server)      │
└────────────┼──────────────────────────────────────────────────┘
             │
      L0 / L1 Agents
```

| 查询类型 | 走哪条路 | 成本 |
|---------|---------|------|
| 「列出所有契约名」「读 task_id=X 的摘要」「查 frontend 域已完成任务」 | Structured Store（精确） | **0 token，毫秒级** |
| 「后端目前的鉴权方案大致是什么？」「跨域有没有和我冲突的设计？」 | LLM Librarian（综合） | 1 次 `query()`，结果缓存 |

### 4.2 数据模型（Structured Store）

```python
from typing import TypedDict, Literal
from datetime import datetime

class RunSummary(TypedDict):
    """结构化运行摘要 —— L0/L1 完成任务后写入"""
    entry_id: str
    layer: Literal["L0", "L1"]        # 谁写的
    agent_id: str
    domain: str                       # frontend|backend|database|global
    task_id: str
    status: Literal["done", "partial", "failed"]
    artifacts: list[str]              # 改动/产出的文件列表
    decisions: list[str]              # 关键决策点（人类可读短句）
    exposes: list[str]                # 对外暴露的接口/能力
    depends_on: list[str]             # 依赖的其他域/契约
    created_at: datetime

class Contract(TypedDict):
    """跨域接口契约 —— L1 注册，Architect 仲裁冲突"""
    name: str                         # 全局唯一键，如 "api.user.login"
    owner_domain: str
    signature: str                    # 接口签名/数据格式
    version: int                      # 同名并发注册 → 版本号递增
    conflict: bool                    # 检测到冲突时置 True，待 Architect 仲裁
    created_at: datetime

class ProjectFact(TypedDict):
    """全局项目事实 —— 技术栈/约定/全局决策"""
    key: str
    value: str
    set_by: str                       # agent_id
    created_at: datetime
```

存储后端：SQLite，路径 `.harness/gch/<project_key>.db`
（`project_key` 由 `claude_agent_sdk.project_key_for_directory(cwd)` 生成）。
→ 跨会话存活，天然衔接 Phase 4 经验沉淀。

### 4.3 MCP 工具表面

通过 `@tool` + `create_sdk_mcp_server(name="gch", ...)` 暴露为进程内 MCP 工具：

| 工具 | 方向 | 谁能用 | 说明 |
|------|------|-------|------|
| `gch_manifest()` | 读 | L0/L1 | **廉价索引**：返回所有可查主题的目录（契约名列表、域、task_id），不返回正文。用于"先列后钻" |
| `gch_get_summary(task_id \| domain)` | 读 | L0/L1 | 精确读结构化摘要（0 token） |
| `gch_list_contracts(domain=None)` | 读 | L0/L1 | 列契约（精确） |
| `gch_get_fact(key)` | 读 | L0/L1 | 读项目事实（精确） |
| `gch_query(question)` | 读 | L0/L1 | **模糊综合**查询，落 LLM 馆员，结果缓存 |
| `gch_write_summary(RunSummary)` | 写 | **仅 L0/L1** | 写结构化运行摘要 |
| `gch_register_contract(Contract)` | 写 | **仅 L1** | 注册契约，自动检测同名冲突 |
| `gch_set_fact(key, value)` | 写 | **仅 L0/L1** | 写项目事实 |

> **L2 Sub/Test Agent 的 `mcp_servers` / `allowed_tools` 里根本不注入任何 `gch_*` 工具** —— 用能力裁剪做物理隔离，而非靠提示词约定。

### 4.4 写权限分层：能力裁剪 > 信任约定

```python
# L1 Main Agent：拿到全套 GCH 工具
main_agent_opts = ClaudeAgentOptions(
    mcp_servers={"gch": gch_server},
    allowed_tools=[
        "Read", "Write", "Edit", "Bash",
        "mcp__gch__gch_manifest", "mcp__gch__gch_get_summary",
        "mcp__gch__gch_list_contracts", "mcp__gch__gch_get_fact",
        "mcp__gch__gch_query",
        "mcp__gch__gch_write_summary", "mcp__gch__gch_register_contract",
        "mcp__gch__gch_set_fact",
    ],
)

# L2 Sub Agent：完全没有 gch_* 工具
sub_agent_opts = ClaudeAgentOptions(
    mcp_servers={},                       # 不挂 GCH
    allowed_tools=["Read", "Write", "Edit", "Bash"],
)
```

> **Phase 3 兜底**：再加 `PreToolUse` hook，按 `agent_type`/`agent_id` 拒绝任何来自 L2 的 `gch_*` 调用，做纵深防御。

---

## 5. 拉取式记忆的纪律（最大风险点的对策）

> ⚠️ **风险**：没有被动投喂，Agent 可能"不知道自己不知道"，于是干脆不查 ——
> 两个 Main Agent 各自定义出冲突的接口，事后才发现。

三重对策：

1. **廉价索引先行**：`gch_manifest()` 只返回目录不返回正文，几乎零成本。鼓励 Agent 频繁"扫一眼有什么"。
2. **「先列后钻」协议**：先 `gch_manifest()` 看有哪些主题 → 命中相关项再 `gch_get_*` 取正文 → 仍不确定才 `gch_query()` 综合。
3. **System Prompt 写死强制动作**（由 Architect/Main Agent 在生成下层提示词时注入）：

```
## 全局记忆使用纪律（强制）
- 设计任何跨域接口前，必须先调用 gch_list_contracts 检查是否已有冲突定义。
- 引用其他域的能力前，必须先 gch_manifest + gch_get_summary 确认其当前状态。
- 完成一个可交付任务后，必须调用 gch_write_summary 写入结构化摘要。
- 不确定全局现状时，用 gch_query 提问，不要凭空假设。
```

4. **存储层强制冲突检测**（更硬的兜底）：`gch_register_contract` 服务端做强制同名检测 → `conflict=True` → 回弹给 Architect，不依赖 Agent 自觉查询。

---

## 6. 节点自治：压缩 / 续接 / 销毁（用 SDK 真实信号）

> ⚠️ **反模式纠正**：旧设计「60% 阈值压缩 `state["messages"]`」是无效的 ——
> 那不是模型真实看到的上下文（见 `.claude/CLAUDE.md` §12）。新方案全部基于 SDK 真实信号。

### 6.1 压缩：真实用量 + PreCompact

```python
# 1. 用真实占用判定，不要手算 count_tokens
usage = await client.get_context_usage()
if usage.percentage > 0.7:          # 阈值是策略，信号必须是真实 percentage
    # 触发 CLI 内部压缩（或 client 侧主动续接）
    ...

# 2. PreCompact hook：压缩前抢救耐久事实
async def pre_compact_hook(inp, tool_use_id, ctx):
    # 把本节点的耐久结论补写进 GCH，避免压缩丢信息
    # （L0/L1 才有 GCH 工具；L2 在此仅做 fold_session_summary）
    return {}

options = ClaudeAgentOptions(
    hooks={"PreCompact": [HookMatcher(matcher=None, hooks=[pre_compact_hook])]},
)
```

要点：
- 阈值（70%）是可调策略；**信号必须是 `get_context_usage().percentage`**，不是手算。
- 压缩是 CLI 内部行为，编排层只在 `PreCompact` 时把耐久事实落 GCH（L0/L1）或折叠摘要（`fold_session_summary`）。

### 6.2 续接 / 温启动

| 场景 | SDK API |
|------|---------|
| 挂起后带记忆续命 | `ClaudeAgentOptions(resume=session_id)` |
| 并行尝试不污染原 session | `fork_session(session_id, up_to_message_id=...)` |
| 列出/恢复历史会话 | `list_sessions` / `get_session_info` / `resume` |
| 挂起池标记 | `tag_session(session_id, "suspended")` |

> 节点续命交给 SDK session 机制；耐久结论已在 GCH，不依赖 session 是否存活。

### 6.3 销毁

```python
await delete_session(session_id)     # 直接删 —— 安全，因为耐久知识已在 GCH
```

销毁是安全的：Agent 的局部对话流水本就该丢，跨域有用的结论早已通过 `gch_write_summary` 沉淀。

---

## 7. 子 Agent 上报：返回值，不是记忆注入

```
L1 Main Agent
   │  ① 派发任务（生成专属 prompt，挂载 cwd / worktree）
   ▼
L2 Sub Agent（独立 session，无 GCH 工具）
   │  ② 执行编码
   │  ③ 产出结构化结果（dataclass / dict）
   │     {files_changed, exposes, notes, status}
   ▼
L1 Main Agent 接收返回值
   │  ④ 开 Test Sub Agent 验收（同样独立 session）
   │  ⑤ 决定是否蒸馏 → gch_write_summary 写入 GCH
   ▼
GCH（仅 L1 写入，L2 全程未接触）
```

- L2 用 `output_format` 强制输出结构化结果，父 L1 直接读结构化返回值，无需解析自然语言。
- L2 文件写入必须按父分配的 **cwd / git worktree** 隔离（并行 L2 写同一目录会冲突 —— 见 §8）。

---

## 8. 并发与一致性

| 问题 | 对策 |
|------|------|
| 多个 L1 并发写 GCH | 进程内单事件循环 + `asyncio.Lock` 串行化写。无分布式问题 |
| 同名契约并发注册 | `gch_register_contract` 检测同名 → `version` 递增 + `conflict=True` → 回传 Architect 仲裁 |
| 并行 L2 写同一文件系统 | 父 L1 按 `cwd` / `git worktree` 给每个 L2 分配隔离工作区；记忆隔离 ≠ 文件系统隔离 |
| GCH 读到半写状态 | SQLite 事务 + 写锁；读永远读已提交版本 |

---

## 9. 可观测性

GCH 的每次读写发射 `ChatEvent`，复用 Phase 1 渲染协议：

| 新增事件 | 触发 | 渲染 |
|---------|------|------|
| `GCH_WRITE` | 任意 `gch_write_*` | 🧠← 显示"X 域写入摘要/契约" |
| `GCH_QUERY` | `gch_query` / `gch_get_*` | 🧠→ 显示"X 向中枢查询：…" |
| `GCH_CONFLICT` | 契约冲突 | ⚠️ 高亮，提示 Architect 仲裁 |

→ 让"记忆总线"的活动在 TUI 里可见，便于调试拉取纪律是否被遵守。

---

## 10. 成本账（相对旧瀑布流）

| 维度 | 旧瀑布流 | 星型记忆 |
|------|---------|---------|
| 向下派发 | 每层打包快照（LLM 提炼） | 0（不投喂，按需拉） |
| 向上回流 | 每层提炼摘要（LLM） | L0/L1 写结构化摘要（多数 0 token，结构化直写） |
| 跨域查询 | 不支持，只能逐层问 | 精确查 0 token / 模糊查 1 次缓存 query |
| 信息损耗 | N 层逐层衰减 | 单跳直达事实源，无衰减 |
| 新增领域 | 改派发/回流链路 | 编排零改动，新 Agent 直连 GCH |

---

## 11. 模块清单（Phase 3+ 实施）

> **Phase 2 对齐**: 根据 `架构师 agent.md` 的 §9 后置清单，GCH 相关模块已正式移至 Phase 3+。
> Phase 2 仅需 L0 Router（`core/architect.py`），不涉及以下任何模块。本表保留作为 Phase 3+ 的实施计划。

| 模块 | 说明 | 阶段 |
|------|------|------|
| `core/context/waterfall.py` | 已废弃（瀑布流管理器） | — |
| `core/context/snapshot.py` | 已废弃（快照生成器） | — |
| `core/context/summary.py` | 已废弃（反向摘要） | — |
| `core/interceptor.py` | 改用 `get_context_usage` + `PreCompact` 自治压缩 | Phase 3+ |
| `core/pool.py` | 改用 SDK `resume`/`fork_session`/`tag_session` | Phase 3+ |
| `core/gch/store.py` | SQLite Structured Store | Phase 3+ |
| `core/gch/server.py` | `@tool` + `create_sdk_mcp_server` 暴露 MCP 工具 | Phase 3+ |
| `core/gch/librarian.py` | 可选 LLM 馆员（模糊查询 + 缓存） | Phase 3+ |
| `core/gch/schema.py` | RunSummary / Contract / ProjectFact | Phase 3+ |

---

## 12. 待确认 / 后续

- **GCH LLM 馆员的模型选择**：模糊查询用便宜模型（如 haiku 档）即可，避免拉高成本。
- **契约冲突仲裁的人机交互**：Architect 仲裁时是否需要 human-in-the-loop 确认（建议 Phase 3 接 `PreToolUse`/权限回调）。
- **GCH 容量治理**：项目级 SQLite 长期增长，Phase 4 引入归档/向量检索时一并处理。
