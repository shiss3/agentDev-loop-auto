# Phase 2 · Step 1 实施计划：BaseAgentSession 抽离

> **状态**：待 review ｜ **范围**：Step 1 仅此一份 ｜ **基线**：B1（SDK==0.2.93）✅ + B2（test_session_send.py 12 测试）✅
> **权威设计源**：`docs/架构师 agent.md` §4.3、`docs/phase_summary_and_phase2_plan.md` §5.2 Step 1

---

## 1. 目标与范围

**一句话**：从 `chat/session.py`（570 行）抽离纯会话管理层 → `core/base_session.py` 的 `BaseAgentSession`；`send()` 改 AsyncIterable 流式 prompt 使 `can_use_tool` 可触发；`permission_mode`/`can_use_tool`/`system_prompt` 提为可配 `__init__` 参数；`ChatSession` 瘦身继承底座，**对外行为零变化**（B2 回归全绿）。

**为什么**：Step 2 的 L0Router 需要一个不绑定 Claude Code preset 的通用会话底座，且要 `permission_mode="default"` + `can_use_tool` 动态权限硬约束（脑手解耦）。这些必须先在底座就位，L0Router 才能直接组合。

**Step 1 不做**（避免越界）：
- L0Router / `architect.py` / 路由 schema / lane 判定 → Step 2
- worktree 隔离 / CLI 子进程并发派发 → Step 3-4
- fast lane `handle_user_input` / 真正的 guard 回调逻辑 → Step 4（Step 1 仅让 `can_use_tool` **可接线**，不写 guard 逻辑）
- 版本号清理 / `core/state.py` 删除 / orchestrator 合并 → Step 6

---

## 2. 文件变更清单

| 文件 | 动作 | 行数估计 | 说明 |
|------|------|---------|------|
| `src/harness_agent/core/base_session.py` | **新建** | ~430 | `BaseAgentSession` + `SessionStats`（从 session.py 搬迁） |
| `src/harness_agent/chat/session.py` | **大改瘦身** | 570 → ~70 | `ChatSession(BaseAgentSession)`，仅留 `_BASE_SYSTEM_PROMPT` + `__init__` 转发 + `_resolve_system_prompt` 覆盖 |
| `src/harness_agent/chat/translator.py` | **小改** | +2 | `TranslationResult.total_cost_usd` 字段 + `_translate_result` 用 `getattr` 提取 |
| `src/harness_agent/core/__init__.py` | **小改** | +1 | 导出 `BaseAgentSession` |
| `tests/test_base_session.py` | **新建** | ~180 | 底座专属测试（can_use_tool 接线、permission_mode、钩子、AsyncIterable、cost） |
| `tests/test_session_send.py` | **不动** | — | B2 回归网，验证 ChatSession 行为不变 |
| `tests/test_session_restore.py` | **不动** | — | 验证构造签名 + `_build_options` 兼容 |

---

## 3. BaseAgentSession 接口设计

### 3.1 `__init__` 参数（在 ChatSession 原 9 参数基础上 +3 个新参数）

```python
def __init__(
    self,
    project_dir: str = ".",
    model: str | None = None,
    max_turns: int | None = None,
    context_provider: ContextProvider | None = None,
    allowed_tools: list[str] | None = None,
    session_store: SessionStore | None = None,
    resume_session_id: str | None = None,
    continue_conversation: bool = False,
    enable_undo: bool = False,
    # ── Step 1 新增：脑手解耦硬约束的落点 ──
    system_prompt: SystemPromptType | None = None,   # L0Router 传 EXECUTOR_PROMPT(str)；ChatSession 传 None
    permission_mode: str = "acceptEdits",             # L0Router 传 "default"；ChatSession 默认 acceptEdits
    can_use_tool: Callable[..., PermissionResult] | None = None,  # L0Router 传 guard 回调；ChatSession 传 None
) -> None
```

> **默认值即 ChatSession 旧行为**：`permission_mode="acceptEdits"` + `can_use_tool=None` + `system_prompt=None`（由子类钩子构建）。因此 ChatSession 转发时无需显式传这三个，走默认即可。

### 3.2 从 ChatSession 原样搬迁的成员

| 成员 | 当前位置 | 迁移后 |
|------|---------|--------|
| `SessionStats` 类 | session.py:83-109 | base_session.py（+`total_cost_usd: float = 0.0`） |
| `_RETRY_PATTERNS` + `_handle_stderr_line` | session.py:193-247 | 原样搬 |
| `is_active`/`is_cancelled`/`is_processing`/`cancel` | session.py:249-269 | 原样搬 |
| `_build_options(for_rebuild)` | session.py:271-350 | 搬 + 参数化（见 §5） |
| `start`/`close` | session.py:352-373 | 原样搬 |
| `_hot_restart_client` | session.py:375-399 | 搬 + query 改 AsyncIterable（见 §4） |
| `_recover_client_after_cancel` | session.py:401-426 | 原样搬 |
| `send(prompt)` | session.py:428-571 | 搬 + query 改 AsyncIterable + 累计 cost（见 §4） |
| 所有实例字段（`_client`/`stats`/`session_id`/`_translator`/`_cancelled`/`_retry_lock`...） | session.py:161-189 | 原样搬 |

### 3.3 扩展钩子

```python
def _resolve_system_prompt(self) -> SystemPromptType:
    """默认返回 system_prompt 参数；子类可覆盖。
    - BaseAgentSession(L0Router 用): 返回 self.system_prompt (EXECUTOR_PROMPT str)
    - ChatSession 覆盖: 返回 context_provider.build_system_prompt(base_prompt=_BASE_SYSTEM_PROMPT, ...)
    """
    return self.system_prompt
```

---

## 4. AsyncIterable send() 改造（§4.3 核心）

### 4.1 问题
当前 `await self._client.query(prompt)` 传**字符串**。SDK 在 `can_use_tool` 被设置时会校验 prompt 必须是 AsyncIterable（`client.py:100-106`，字符串 → `ValueError`）。要让底座支持 `can_use_tool`，必须改流式 prompt。Spike G 已验证：AsyncIterable + acceptEdits 共存正常，故对 ChatSession（无 can_use_tool）透明无副作用。

### 4.2 提取 helper（send 与 _hot_restart_client 共用，避免重复）

```python
@staticmethod
def _to_prompt_stream(prompt: str):
    """字符串 → SDK 流式 prompt（AsyncIterable）。
    使 can_use_tool 在 ask 态触发；对 acceptEdits 模式无副作用。
    格式来自 SDK 内部 string→message 包装 (client.py:215-224)，Spike G 已验证。"""
    async def _stream():
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        }
    return _stream()
```

### 4.3 两处调用点同步改造

| 位置 | 旧 | 新 |
|------|----|----|
| `send()` L471 | `await self._client.query(prompt)` | `await self._client.query(self._to_prompt_stream(prompt))` |
| `_hot_restart_client()` L395 | `await self._client.query(f"[系统] ...")` | `await self._client.query(self._to_prompt_stream(f"[系统] ..."))` |

> **关键**：`_hot_restart_client` 的 query 调用必须同步改，否则 L0Router 带 can_use_tool 时热重启会 ValueError。ChatSession 无 can_use_tool，字符串本可工作，但底座必须一致用 AsyncIterable。

### 4.4 send() 其余逻辑
原样保留：TURN_START/yield、retry 计数跨线程检测、取消中断、translator 转换、token 累计、on_turn_end 通知、`has_context_changed()` 触发热重启、TURN_END。**唯一新增一行**：`self.stats.total_cost_usd += result.total_cost_usd`。

---

## 5. _build_options 参数化

```python
def _build_options(self, for_rebuild: bool = False) -> ClaudeAgentOptions:
    system_prompt = self._resolve_system_prompt()        # ← 钩子，替代硬编码 context_provider 调用

    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=self.project_dir,
        allowed_tools=self.allowed_tools,
        max_turns=self.max_turns,
        permission_mode=self.permission_mode,            # ← 可配（原硬编码 "acceptEdits"）
        include_partial_messages=True,
        stderr=self._handle_stderr_line,
    )

    if self.can_use_tool is not None:                    # ← 新增：仅当传入时挂载
        opts.can_use_tool = self.can_use_tool

    # ── 以下原样保留：enable_undo / session_store / session_id 分支 ──
    if self.enable_undo:
        opts.enable_file_checkpointing = True
    elif self.session_store:
        opts.session_store = self.session_store
    if self.model:
        opts.model = self.model
    # session_id / resume / continue_conversation 分支（for_rebuild/enable_undo/resume/continue/new）原样保留
    ...
    return opts
```

**改动点仅 3 处**：system_prompt 走钩子、permission_mode 走参数、can_use_tool 条件挂载。其余 session_id/resume/continue/checkpoint 逻辑**逐字保留**（test_session_restore 覆盖）。

---

## 6. ChatSession 瘦身后（~70 行）

```python
"""ChatSession — 基于 BaseAgentSession 的多轮对话会话。
在底座之上：用 ContextProvider 构建 claude_code preset system_prompt；保留 enable_undo 检查点模式。"""
from __future__ import annotations
from harness_agent.context.provider import ContextProvider, SystemPromptType
from harness_agent.core.base_session import BaseAgentSession

_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。..."""  # 保留在 chat 层（Claude Code 专属基提示）

class ChatSession(BaseAgentSession):
    def __init__(self, project_dir=".", model=None, max_turns=None,
                 context_provider=None, allowed_tools=None, session_store=None,
                 resume_session_id=None, continue_conversation=False, enable_undo=False):
        super().__init__(
            project_dir=project_dir, model=model, max_turns=max_turns,
            context_provider=context_provider, allowed_tools=allowed_tools,
            session_store=session_store, resume_session_id=resume_session_id,
            continue_conversation=continue_conversation, enable_undo=enable_undo,
            # 三个新参数走默认：system_prompt=None / permission_mode="acceptEdits" / can_use_tool=None
            # → ChatSession 行为与重构前完全一致
        )

    def _resolve_system_prompt(self) -> SystemPromptType:
        return self.context_provider.build_system_prompt(
            base_prompt=_BASE_SYSTEM_PROMPT, project_dir=self.project_dir,
        )
```

**兼容性保证**：
- `ChatSession(project_dir=".")` ✅（repl.py:91、test_session_restore.py、test_session_send.py 全部不变）
- `_build_options()` 仍可外部调用 ✅（test_session_restore 断言）
- 产出事件流、acceptEdits、无 can_use_tool、claude_code preset —— 全部不变 ✅

---

## 7. SessionStats + translator 加 total_cost_usd

| 文件 | 改动 |
|------|------|
| `base_session.py` SessionStats | +`self.total_cost_usd: float = 0.0`；`to_dict()` 加一项 |
| `translator.py` TranslationResult | +`total_cost_usd: float = 0.0` |
| `translator.py` `_translate_result` | `result.total_cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)` |
| `base_session.py` send() finally 前 | `self.stats.total_cost_usd += result.total_cost_usd` |

> ⚠️ **语义待核**：`ResultMessage.total_cost_usd` 是"单轮"还是"会话累计"在 SDK 0.2.93 未明确。Step 1 用 `getattr` 安全提取并**累加**；若 Step 2 L0Router 预算追踪发现是累计语义，改为赋值即可。低风险。

---

## 8. 测试策略

### 8.1 B2 回归网（必须全绿，零修改）
`tests/test_session_send.py` 12 测试 —— 现在测的是 `ChatSession(BaseAgentSession).send()`。因 mock 的 `client.query` 是 AsyncMock（不校验参数类型），AsyncIterable 改造对其透明。**run 全绿即证明行为不变**。

### 8.2 现有兼容性测试（必须全绿，零修改）
`tests/test_session_restore.py`：`test_create_new_session` 调 `ChatSession(project_dir=".")` + `_build_options()`，验证构造签名与 options 构建未破坏。

### 8.3 新增 `tests/test_base_session.py`（底座专属，~7 测试）

| 测试 | 断言 |
|------|------|
| `test_default_permission_mode_accept_edits` | `BaseAgentSession()._build_options().permission_mode == "acceptEdits"` |
| `test_permission_mode_configurable` | `BaseAgentSession(permission_mode="default")._build_options().permission_mode == "default"` |
| `test_can_use_tool_propagated` | 传 `can_use_tool=cb` → `opts.can_use_tool is cb`；不传 → opts 无该属性 |
| `test_resolve_system_prompt_default` | `BaseAgentSession(system_prompt="X")._resolve_system_prompt() == "X"` |
| `test_resolve_system_prompt_subclass_override` | 子类覆盖 → 返回自定义值（验证钩子可扩展） |
| `test_send_uses_asynciterable_prompt` | mock client，`send("hi")` 后 `client.query` 收到的是 async generator；迭代它 yield 出 `{"type":"user","message":{"role":"user","content":"hi"},...}`（非字符串） |
| `test_total_cost_usd_accumulated` | mock ResultMessage 带 `total_cost_usd=0.5`，`send()` 后 `stats.total_cost_usd == 0.5` |

### 8.4 手动冒烟（交用户验证）
`harness chat` 实跑一轮，确认真实 SDK 下流式渲染、工具调用、取消、`/context` 统计均正常。（不写自动化集成测试，避免 CI 依赖真实 CLI。）

---

## 9. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| AsyncIterable 改造破坏 ChatSession | 中 | B2 12 测试 + Spike G 验证；AsyncMock 透明 |
| `_hot_restart_client` query 漏改 → L0 阶段 ValueError | 中 | §4.3 两处同步改 + `_to_prompt_stream` helper |
| session_id/resume/enable_undo 逻辑搬迁出错 | 中 | 逐字保留 + test_session_restore 覆盖 |
| import 环：core.base_session → chat.events/translator + context.provider；chat.session → core.base_session | 低 | 已确认 events/translator/provider 不反向导入 session，无环 |
| 分层倒置：core 依赖 chat | 低 | doc 指定 `core/base_session.py`；仅依赖稳定的 ChatEvent/translator 协议，可接受。若 review 不喜可改放 `chat/base_session.py`（待决） |
| `total_cost_usd` 语义不明 | 低 | getattr 安全提取，Step 2 再定 |
| 预存在死代码（`tool_result_event` 未用导入、retry 正则不匹配文档格式） | — | **不动**（Karpathy #3：原有死代码不清理）；搬迁时自然不携带 `tool_result_event` 导入 |

---

## 10. 执行工作流（按用户指示）

1. **我派发 subagent 执行**：本 Step 1 拆为 2 个 subagent 任务（见下），各自独立完成 + 自审。
2. **每个 subagent 完成后自审**：调用 `@.claude/agents/code-review-agent.md` review 自己的改动，产出 APPROVE/WARNING/Block + 严重度。减少主上下文噪音。
3. **我汇总**：收 subagent 的 review 结论 + 我跑一次全量测试确认绿，再向你交付。

### 任务拆分
- **任务 A**：新建 `core/base_session.py`（BaseAgentSession + SessionStats）+ 改 `translator.py`（+total_cost_usd）+ 改 `core/__init__.py`。**不**动 session.py。自审。
- **任务 B**（依赖 A）：瘦身 `chat/session.py` → ChatSession(BaseAgentSession)。自审。完成后跑 `tests/test_session_send.py` + `tests/test_session_restore.py` + `tests/test_base_session.py` 全绿。

---

## 11. 待你 review 时一并决策

1. **分层倒置**：BaseAgentSession 放 `core/base_session.py`（doc 指定）还是 `chat/base_session.py`（避免 core→chat 反向依赖）？我倾向遵循 doc 放 core/。
2. **code-reviewer 模型**：`code-review-agent.md` frontmatter `model: sonnet` 在本环境解析为 glm-5.1（不可用）。B2 时 subagent 被迫降级用 general-purpose 完成 review。请示下：
   - (a) 授权我改 `code-review-agent.md` 的 `model` 字段为可用模型（如 opus / 当前会话模型）；或
   - (b) 你指定每次 review 用哪个模型；或
   - (c) 维持现状（general-purpose 兜底，review 仍有实质内容但非专属 reviewer）。

---

## 12. 验收标准（Step 1 完成 = 全部满足）

- [ ] `core/base_session.py` 存在，`BaseAgentSession` 含 §3 全部成员 + 3 个新参数 + `_resolve_system_prompt` 钩子 + `_to_prompt_stream` helper
- [ ] `send()` 与 `_hot_restart_client()` 的 query 调用均用 AsyncIterable
- [ ] `ChatSession` 继承 BaseAgentSession，对外签名/行为不变
- [ ] `tests/test_session_send.py` 12 测试全绿（零修改）
- [ ] `tests/test_session_restore.py` 全绿（零修改）
- [ ] `tests/test_base_session.py` 7 测试全绿
- [ ] `harness chat` 手动冒烟通过（用户确认）
- [ ] 两个 subagent 各自产出 code-review 结论
