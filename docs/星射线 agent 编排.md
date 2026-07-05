# 🌟 星射线智能体编排系统 (Star-Ray Agent Orchestration) — 远期愿景

> **文档日期**: 2026-07-04
>
> **阅读提示（重要）**：本文档为星射线三层编排的**远期愿景文档（Future Vision）**，描述完整的"入口架构师 → 主智能体 → 子智能体"三层架构理念与各层职责。
>
> **修订要点**：本文档已重新定位为**远期愿景**。完整三层编排（自建 L1/L2、GCH、挂起池、温启动、脑手解耦完整形态）是长期目标，但 Phase 2 采用极简 Router 路径（见 `架构师 agent.md`）。新增 §四「愿景与现实的桥接」解释两版设计的关系与演进路径。

---

## 摘要 (Executive Summary)

> **远期愿景**：本文档描述的是星射线架构的**终极形态**——完整的三层编排（入口架构师 → 主智能体 → 子智能体）、GCH 全局上下文中枢、挂起池温启动、脑手解耦等全套机制。这是系统的**长期演进方向**。
>
> **Phase 2 不做这些**。当前 Phase 2 实际实施的是极简 Router 方案（见 [`架构师 agent.md`](./架构师%20agent.md)）：L0 仅做 lane 判定（Fast/Full），Fast 自执行，Full 用 `claude code` CLI 子进程 + git worktree。**不在此阶段自建 L1/L2、GCH、挂起池**。这些能力将在 Phase 3+ 按需演进。
>
> 下面描述的是星射线架构的**完整蓝图**，三层分形放射拓扑是我们的北极星方向。

在历经单 Agent MVP（Phase 1）的验证后，系统的编排层正式升级为**"星射线智能体编排"**架构。该架构采用**三层分形放射状 (3-Tier Fractal Radial)**拓扑，将系统算力收敛为"核心规划"与"泛型执行"两部分。通过极致的"脑手解耦"、**星型记忆拓扑**（节点记忆自治 + GCH 单点收敛）以及动态的挂起与压缩机制，彻底解决传统多智能体系统中的上下文爆炸、状态管理混乱和能力浪费等痛点，实现真正的工程级长生命周期自主开发。

---

## 一、 系统架构拓扑 (三层分形架构)

整个编排系统呈现由中心向外发散的"星射线"状，仅在射线末端（代码生成与测试）保留微循环闭环。

### 1. 👑 核心节点 (The Core)：入口架构师 (Architect Agent)
* **生命周期**：贯穿整个项目的开发生命周期。
* **核心职责**：
    * 作为系统的单一入口，接收用户的宏观需求。
    * 拥有全局项目目录视野。
    * 进行任务拆解与类型判定，决定将任务分配给已有的领域主智能体，或按需开启新的主智能体。
    * 充当全局路由分发器。
* **限制**：绝对不参与任何具体代码的编写与底层 API 调用。

### 2. 🛡️ 光芒骨干 (The Rays)：主智能体 (Main Agents)
* **生命周期**：随用户当前开发 Session 存在，仅当任务完全完成且用户主动要求关闭时才被销毁。
* **核心职责**：
    * 担任特定领域（如前端、后端、数据库等）的"主理人"。
    * 独立运行，相互之间解耦，通过 **GCH（全局上下文中枢）** 查询和共享跨域信息，而非直接继承其他节点的记忆快照。
    * 负责领域内的任务排期、系统架构设计、接口契约制定以及边界把控。
    * 作为子智能体 (Sub Agents) 的工厂，向外发射具体的编码与测试任务。
* **限制**：**不写代码，不调用底层 SDK 修改文件。** 这保证了它的上下文消耗极慢，保持清爽的"上帝视角"。

### 3. ⚔️ 繁星执行者 (The Stardust)：子智能体 (Sub / Test Agents)
* **生命周期**：动态按需生成 (Spawn-on-Demand)，执行完毕后挂起休眠，超载时销毁重建。
* **核心职责**：
    * **编码子 Agent**：纯粹的"打字机"与"执行者"。基于 `claude-agent-sdk` 的通用执行引擎，被主智能体赋予特定人设和局部视野后，独立调用工具读写文件。
    * **测试子 Agent**：负责在编码 Agent 修改完成后，拉起底层测试靶场或验证脚本进行检验。
* **工作流闭环**：主 Agent 下发任务 -> 子 Agent A 写代码 -> 主 Agent 唤醒测试 Agent B -> 检验通过则挂起 A，失败则打回 A 重写。

---

## 二、 核心机制设计

### 1. 动态衍生与并行路由 (Dynamic Spawning & Map-Reduce)

利用 `asyncio.gather` 进行动态并行路由。架构师拆解任务后，系统按 DAG 依赖分批并行拉起多个 Main Agent：

```python
# 按 DAG 批次并行执行 L1
batch = scheduler.ready_batch()
results = await asyncio.gather(*[
    run_main_agent(t, plan.prompts[t.domain], project_dir)
    for t in batch
])
```

L1 内部同样通过 `asyncio.gather` 并行发射 L2 子 Agent。

### 2. 记忆与上下文共享机制 (Context Management)

> 完整设计见 [`memory_system_design.md`](./memory_system_design.md)。以下为概念级概述。

为防止子 Agent 海量的底层 diff 日志反向污染主 Agent，系统采用**星型记忆拓扑**：

* **节点记忆自治**：每个 Agent 持有独立的 SDK session，各自完整持有自身对话历史与上下文。互不直接注入、互不交叉修改。
* **全局中枢（GCH）单点收敛**：全项目级、跨领域的公共上下文，**只**收敛到 GCH 一个节点。不存在任何 Agent 之间的直接记忆传递。
* **按需拉取，零冗余**：系统不做被动上下文投喂。Agent 需要全局/跨域信息时，主动通过 MCP 工具向 GCH 查询；不需要则零加载。
* **L2 完全隔离**：子 Agent 不接触 GCH，执行结果通过结构化返回值回传给父 L1，由 L1 决定是否蒸馏后写入 GCH。

### 3. 对象池与温启动机制 (Agent Pool & Warm Start)

> 基于 SDK 原生 session 机制。完整设计见 [`memory_system_design.md`](./memory_system_design.md) §6。

* **挂起 (Suspend)**：当子 Agent 完成任务且测试通过后，利用 SDK 的 `session_store` + `tag_session` 将会话标记为挂起状态。
* **温启动 (Warm Start)**：当后续有类似模块的修改任务时，主 Agent 通过 `resume` 或 `ClaudeSDKClient` 恢复 SDK 真实 session。子 Agent 带着之前的**局部领域记忆**继续开发。

### 4. 阈值压缩与生命周期边界 (Threshold Compression & Lifecycle)

针对子 Agent 长期复用带来的上下文膨胀问题，引入**基于 SDK 真实用量的上下文管理**：

* **自动压缩为主**：信任 SDK 原生自动压缩（`isAutoCompactEnabled`），CLI 内部在接近阈值时自动处理。
* **PreCompact 钩子注入**：在 SDK 即将压缩上下文前，通过 `PreCompact` hook 将领域关键事实写入 GCH 或折叠摘要，防止核心经验被压没。
* **KILL 兜底**：当 `get_context_usage().percentage` 逼近上限且自动压缩被禁用时，执行摘要交接 → 销毁旧 session → 冷启动新 session。

---

## 三、 预期工程收益

1. **解决上下文雪崩**：主脑不看 diff，手脚定期压缩。将宝贵的长上下文全部分配给系统设计与架构验证。
2. **降低幻觉的局部专注**：每次派发的子任务都拥有专属定制的 System Prompt，Agent 不会被全量代码库干扰，专注眼前的 Bug 和 Feature。
3. **架构的极致简洁**：编排层零框架依赖（纯 async Python）。增加新语言或新栈，编排层代码 **零修改**，完全依赖大模型的动态认知。
4. **经验的无缝沉淀**：结合挂起机制与最终的 `.skills` 提取，系统真正具备了"随工作时长增长而变强"的类人特性。

---

## 四、 愿景与现实的桥接：从极简 Router 到完整三层

> 本节解释 [`架构师 agent.md`](./架构师%20agent.md)（Phase 2 实际落地）与本文档（远期愿景）之间的关系与演进路径。

### 4.1 两版设计对照

| 维度 | Phase 2 极简 Router | 远期完整三层（本文档） |
|------|--------------------------|---------------------|
| **L0 角色** | 极薄 Router：仅 lane 判定 + 派发/自执行 | 入口架构师：任务拆解、DAG 排期、全局路由分发 |
| **L1** | 不自建 — Full 模式交给 `claude code` CLI 子进程（自带 subagent） | 自建主智能体：领域主理人、契约制定、子 Agent 工厂 |
| **L2** | 不自建 — `claude code` 内部 subagent 机制接管 | 自建子智能体：编码 + 测试，动态 spawn，挂起/销毁 |
| **GCH** | 不做 | 全局上下文中枢：MCP 工具 + SQLite 事实源 |
| **脑手解耦** | Fast Lane 不适用（L0 兼执行体）；Full 模式用 `can_use_tool` 禁业务写 | 完整形态：L0 只读，L1 只读，L2 可写 |
| **挂起池/温启动** | 不做 | SDK session_store + tag/resume |
| **上下文管理** | 依赖 SDK 自动压缩 | PreCompact + get_context_usage + KILL 兜底 |
| **并行策略** | Full: asyncio.gather(CLI 子进程 × N)，各 worktree 独立 | L1: DAG batch 并行; L2: asyncio.gather 子 Agent |
| **冲突协调** | git merge 冲突标记为"需人工介入" | 同域收敛、资源声明、三级防线兜底 |

### 4.2 为什么 Phase 2 选极简 Router 而不是直接上完整三层

1. **先跑通骨架**：在验证完整三层的价值之前，先让"一个入口 → 自动判定 → 执行"的最小闭环跑通。极简 Router 是完整三层的一个**可工作的子集**——Fast Lane 等价于 L0 自执行（跳过 L1/L2），Full 等价于 L0 派发（对外部执行体的调度）。
2. **复用优于自建**：`claude code` 自带的 subagent 机制已经很完善，Phase 2 不自建 L1/L2，避免过早抽象。等到真正需要"跨领域契约协调"、"子 Agent 跨任务经验沉淀"等高级能力时再自建。
3. **降低验证成本**：完整三层需要 GCH、挂起池、脑手解耦、三级防线等大量基础设施才能跑出效果。极简 Router 版本只需 ~250 行核心代码，快速验证路由判定 + Fast/Full 双 lane 的可行性。
4. **按需演进**：完整三层是北极星方向，但每一层能力的引入都应该由**真实的瓶颈**驱动——当 Full 模式下 claude code 子进程之间需要协调契约时引入 GCH，当子 Agent 反复冷启动浪费 token 时引入挂起池，当 Fast Lane 长任务撑爆上下文时引入 PreCompact。

### 4.3 演进路径

```
Phase 2 (当前)
  极简 Router
  ├─ Fast Lane: L0 自执行
  └─ Full: claude code CLI 子进程 + worktree
      │
      ▼ (当 Full 模式下子进程之间出现跨领域契约协调需求)
Phase 3+
  + GCH 全局上下文中枢
  + Full 模式 L0 收紧为只读（脑手解耦完整形态）
  + merge 冲突自动解决子进程
      │
      ▼ (当需要跨任务经验沉淀、温启动降低 token 成本)
Phase 3+
  + 自建 L1 主智能体（领域主理人）
  + 自建 L2 子智能体（动态 spawn/挂起/销毁）
  + 挂起池 + 温启动
  + PreCompact 上下文压缩兜底
      │
      ▼ (当出现跨需求硬依赖)
Phase 4+
  + GlobalDAG 跨需求依赖协调
  + Skill 知识库提取
  + 三级防线同域收敛
```

### 4.4 三层架构的角色在未来如何演化

当完整三层建成后,L0 从极简 Router 演化为一个**更智能的入口架构师**——它不仅做 lane 判定，还负责：
- 对 Full 任务进行 DAG 拆解与依赖编排
- 管理跨领域契约（通过 GCH）
- 动态创建/回收 L1 主智能体

L1 主智能体和 L2 子智能体届时接管当前 Phase 2 中 `claude code` CLI 子进程的职责，但提供更细粒度的控制：领域记忆沉淀、跨任务经验复用、精准的上下文生命周期管理。

---

## 五、 相关文档

| 文档 | 定位 | 阶段 |
|------|------|------|
| [`架构师 agent.md`](./架构师%20agent.md) | **Phase 2 实际落地设计** — L0 极薄 Router 方案 | **Phase 2** |
| [`phase_summary_and_phase2_plan.md`](./phase_summary_and_phase2_plan.md) | Phase 1 总结 + Phase 2 完整实现计划（含代码示例） | Phase 2 |
| [`memory_system_design.md`](./memory_system_design.md) | 星型记忆拓扑 + GCH 完整设计（Phase 3+ 启用） | Phase 3+ |
| [`critical_review.md`](./critical_review.md) | Red Team 尖锐评审备忘 | — |
| [`.claude/CLAUDE.md`](../.claude/CLAUDE.md) | `claude-agent-sdk` v0.2.93 能力速查 | — |