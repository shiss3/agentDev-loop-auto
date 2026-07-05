# Phase 2 Step 2 实施计划 — L0 Router 核心（路由判定 + 底座组合）

> **范围**：新建 `core/architect.py`（L0Router）+ `core/utils.py`（`_build_message`），实现路由判定 + Fast Lane 自执行 + 路由降级 + 动态权限硬约束回调。Full 模式（`_run_full`/`_spawn_cli`/worktree）占位，Step 4 实现。
>
> **权威设计**：以 [`docs/架构师 agent.md`](./架构师%20agent.md) §3-§5 为准。本计划为实施细化。
>
> **前置**：Step 1 已完成 `BaseAgentSession` 底座（含 `permission_mode`/`can_use_tool`/`system_prompt` 参数 + `_to_prompt_stream` AsyncIterable 改造）。

---

## 1. 范围边界

### Step 2 做
| 项 | 落点 |
|---|---|
| L0Router 类骨架 + 极简状态（`_current_lane`/`project_dir`/`model`/`_session`） | `core/architect.py` |
| 常驻执行 session 组合（`_build_resident_session`：EXECUTOR_PROMPT + `permission_mode="default"` + `can_use_tool=_lane_guard` + 业务写不进 `allowed_tools`） | `core/architect.py` |
| 路由判定 `_route()`：独立 stateless `query()` 顶层 API + `output_format`，不经过 BaseAgentSession | `core/architect.py` |
| `handle_user_input(text, *, forced_lane)` 返回 `AsyncIterator[ChatEvent]`：自动路由 / forced fast / forced full + 降级 | `core/architect.py` |
| 动态权限硬约束 `_lane_guard`：fast 放行、full/None 禁业务写、Bash 始终放行 | `core/architect.py` |
| 路由降级：`_route` 异常 → 降级 Fast Lane 执行，不阻断用户 | `core/architect.py` |
| `session` property 穿透、`rebuild()`、`start()`/`close()` | `core/architect.py` |
| `_build_message()` 消息工厂（薄封装 `text_event`） | `core/utils.py` |
| `core/__init__.py` 导出 `L0Router` | `core/__init__.py` |
| 单测：路由判定 / handle_user_input 各分支 / 降级 / _lane_guard / 入口重置 / resident 配置 | `tests/test_l0_router.py` + `tests/test_utils.py` |

### Step 2 不做（明确排除，避免死代码 — Karpathy #3）
| 排除项 | 落点 |
|---|---|
| `_run_full` 真实实现（CLI 子进程 + worktree + merge） | Step 4 — Step 2 占位 yield 提示消息 |
| `_spawn_cli` / `_resolve_cli_path` / `_extract_result_text`/`_extract_cost`/`_extract_usage` | Step 4（`core/cli_utils.py`） |
| worktree 函数（`create_worktree`/`merge_worktree`/`remove_worktree`） | Step 4（`core/worktree.py`） |
| REPL 集成 / 斜杠命令 `/fast`/`/full` / `_full_active` 检测 | Step 5 |
| `_total_cost`/`_full_cost`/`_full_tokens`/`_worktree_registry`/`CLI_TIMEOUT` 字段 | Step 4/5 用到时再加 — Fast cost 由 `session.stats.total_cost_usd` 自管（架构师.md §7） |
| 上下文压缩 / 预算控制 / GCH | Phase 3+ |

---

## 2. 接口契约（精确，Task A 实现依此，Task B 测试依此）

### 2.1 `core/utils.py`

```python
"""L0Router 共用消息构造工具。"""
from harness_agent.chat.events import ChatEvent, text_event


def _build_message(text: str, agent: str = "default") -> ChatEvent:
    """构造 L0Router 系统消息（路由提示、降级提示、Full 占位等）。

    薄封装 text_event —— 统一 L0Router 消息构造入口，未来可扩展（带 agent 名/metadata）。
    """
    return text_event(text, agent=agent)
```

### 2.2 `core/architect.py`

#### 常量

```python
BUSINESS_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
# Bash / Read / Glob / Grep 始终放行（运维 + 只读）

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

ROUTE_JUDGE_PROMPT = """\... """   # 见架构师.md §3（判定员角色 + 规则 + 纪律）
EXECUTOR_PROMPT = """\... """      # 见架构师.md §5（L0 Executor 角色）
```

#### `L0Router` 类

```python
class L0Router:
    """L0 极薄 Router。组合常驻 BaseAgentSession + 独立路由判定。

    - 路由判定走 query() 顶层 API（stateless，不进常驻 session 历史，不持久化）
    - Fast Lane 经 self._session.send(text) 产出 ChatEvent 流（text 首次进常驻 session）
    - Full 模式 Step 4 实现，本步占位
    - 动态权限：常驻 session permission_mode="default" + 业务写不进 allowed_tools
      → 强制走 ask 态触发 _lane_guard 按 lane 放行/拒绝（Spike G 验证）
    """

    def __init__(
        self,
        project_dir: str,
        model: str | None = None,
        *,
        session_store: SessionStore | None = None,
    ) -> None:
        self._current_lane: str | None = None     # "fast" | "full" | None
        self.project_dir = project_dir
        self.model = model
        self._session_store = session_store if session_store is not None else create_session_store()
        self._session = self._build_resident_session()

    @property
    def session(self) -> BaseAgentSession:
        """穿透到常驻底座（commands.py 的 cli.session.* 零改动，Step 5 用）。"""
        return self._session

    def _build_resident_session(self) -> BaseAgentSession:
        """构造常驻执行 session（__init__ 与 rebuild 共用）。

        硬约束配置（架构师.md §4.2 / Spike G）：
        - system_prompt=EXECUTOR_PROMPT（str，_resolve_system_prompt 钩子默认直返）
        - permission_mode="default"（非 acceptEdits，否则 deny 被压过）
        - allowed_tools=["Read","Glob","Grep","Bash"]（业务写不进 → 走 ask 态触发回调）
        - can_use_tool=self._lane_guard
        - session_store=self._session_store（持久化，跨进程可 resume）
        """
        return BaseAgentSession(
            project_dir=self.project_dir,
            model=self.model,
            system_prompt=EXECUTOR_PROMPT,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode="default",
            can_use_tool=self._lane_guard,
            session_store=self._session_store,
        )

    async def _lane_guard(
        self, tool_name: str, tool_input: dict[str, Any], ctx: Any
    ) -> PermissionResult:
        """can_use_tool 回调（必须 async —— CanUseTool 返回 Awaitable[PermissionResult]）。

        签名 (tool_name, tool_input, ctx) 对齐 SDK types.py:253。
        - fast → Allow（业务写放行）
        - full/None（异常残留）→ Deny 业务写（L0 不得直接编辑代码文件）
        - 非业务写工具（Bash/Read/...）一律 Allow（本就预批准，不会进本回调，防御性放行）
        """
        if self._current_lane != "fast" and tool_name in BUSINESS_WRITE_TOOLS:
            return PermissionResultDeny(
                message="当前模式:业务代码修改由 claude code 子进程执行,L0 不得直接编辑代码文件。",
            )
        return PermissionResultAllow()

    async def start(self) -> None:
        await self._session.start()

    async def close(self) -> None:
        await self._session.close()

    async def rebuild(
        self, *, project_dir: str | None = None, model: str | None = None
    ) -> None:
        """重建常驻 session（switch_project/switch_model/reset_session 调用，Step 5 用）。

        ⚠️ 调用方（Step 5 ChatCLI.switch_*）须先检测 _full_active（Step 5 加）。
        """
        if project_dir is not None:
            self.project_dir = project_dir
        if model is not None:
            self.model = model
        await self._session.close()
        self._session = self._build_resident_session()
        await self._session.start()

    async def _route(self, text: str) -> dict:
        """独立 stateless 路由判定。query() 顶层 API + output_format，不新建 BaseAgentSession。

        - tools=[]（彻底禁用内置工具，纯分类任务，SDK 映射 --tools ""）
        - max_turns=3（限制路由判定回合）
        - 不挂 session_store（一次性，用完即弃，ADR-14）
        - 不挂 can_use_tool（路由判定无工具调用）
        返回 ResultMessage.structured_output（dict）。未返回结构化结果 → RuntimeError（被 handle_user_input 捕获降级）。
        """
        opts = ClaudeAgentOptions(
            system_prompt=ROUTE_JUDGE_PROMPT,
            tools=[],
            output_format={"type": "json_schema", "schema": ROUTER_DECISION_SCHEMA},
            cwd=self.project_dir,
            model=self.model,
            max_turns=3,
        )
        async for msg in query(prompt=text, options=opts):
            if isinstance(msg, ResultMessage) and msg.structured_output:
                return msg.structured_output
        raise RuntimeError("路由判定未返回结构化结果")

    async def handle_user_input(
        self, text: str, *, forced_lane: str | None = None
    ) -> AsyncIterator[ChatEvent]:
        """用户输入 → ChatEvent 流。

        forced_lane 语义（B2）：
          None    → 自动路由（_route 判定 lane + 拆任务）
          "fast"  → 跳过 _route，直接 Fast Lane 执行
          "full"  → 仍调 _route 拆域任务，但强制 lane="full"
        """
        # 1. 入口设定 lane（forced fast 直接定；自动/forced full 先清 None —— 异常残留兜底）
        if forced_lane == "fast":
            self._current_lane = "fast"
        else:
            self._current_lane = None

        # 2. 路由判定
        if forced_lane == "fast":
            decision = {"lane": "fast", "reason": "用户强制 /fast", "tasks": []}
        else:
            try:
                decision = await self._route(text)
            except Exception as e:
                yield _build_message(
                    f"⚠️ 路由判定异常({str(e)[:80]}),已降级为单领域模式执行。"
                    f"若需求确为多领域,重新发送一次即可重新判定。"
                )
                self._current_lane = "fast"
                async for event in self._session.send(text):
                    yield event
                return
            if forced_lane == "full":
                decision["lane"] = "full"   # 强制 full：仅覆盖 lane，保留 _route 拆出的 tasks

        lane = decision.get("lane", "fast")
        self._current_lane = lane

        if forced_lane is None:
            yield _build_message(f"🔀 路由判定: {lane} ({decision.get('reason', '')})")
        else:
            yield _build_message(f"🔀 强制车道: {lane}")

        # 3. 执行
        if lane == "fast":
            async for event in self._session.send(text):
                yield event
            return

        # lane == "full" — Step 4 实现，本步占位
        async for event in self._run_full(decision.get("tasks", [])):
            yield event

    async def _run_full(self, tasks: list[dict]) -> AsyncIterator[ChatEvent]:
        """Full 模式：CLI 子进程 + worktree 隔离 + merge 收口。Step 4 实现。

        本步占位：路由判定能跑通（产出 lane=full + tasks），但执行体待 Step 4。
        """
        yield _build_message(
            f"🌐 识别为多领域需求（{len(tasks)} 个任务），Full 模式将在 Step 4 实现"
            f"（CLI 子进程 + worktree 隔离）。当前请用 /fast 单领域执行。"
        )
```

#### 导入

```python
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SessionStore,
    query,
)

from harness_agent.chat.events import ChatEvent
from harness_agent.chat.session_store import create_session_store
from harness_agent.core.base_session import BaseAgentSession
from harness_agent.core.utils import _build_message
```

### 2.3 `core/__init__.py`

```python
"""Harness Agent Core"""

from harness_agent.core.architect import L0Router as L0Router
from harness_agent.core.base_session import (
    BaseAgentSession as BaseAgentSession,
    SessionStats as SessionStats,
)
```

---

## 3. 测试策略

### `tests/test_utils.py`
- `test_build_message`：`_build_message("hi")` → `ChatEvent(type=TEXT, data={"text": "hi"})`
- `test_build_message_agent`：`_build_message("hi", agent="l0")` → `agent == "l0"`

### `tests/test_l0_router.py`

mock 策略（不依赖真实 SDK/CLI）：
- `_route` 测试：`patch("harness_agent.core.architect.query")` 为 fake async generator，yield 携带 `structured_output` 的 fake `ResultMessage`
- `handle_user_input` Fast 分支：`patch.object(router._session, "send")` 为 fake async generator 产出 fake `ChatEvent`；`patch.object(router, "_route")` 返回 fake decision
- `_lane_guard`：直接 `await router._lane_guard(...)`，断言返回 `PermissionResultAllow`/`Deny`
- `_build_resident_session` 配置：构造 L0Router（不 start），检查 `router._session` 的属性

测试点：

| # | 测试 | 断言 |
|---|---|---|
| 1 | `test_route_returns_structured_output` | mock query 返回 `{lane:"fast",...}`，`_route()` 返回该 dict |
| 2 | `test_route_no_structured_output_raises` | mock query 不返回 structured_output → `RuntimeError`（被 handle_user_input 捕获） |
| 3 | `test_handle_fast_lane_auto` | mock `_route`→fast + mock `send`；events 含路由提示 + send 的事件；`_current_lane=="fast"` |
| 4 | `test_handle_full_lane_placeholder` | mock `_route`→full+tasks；events 含路由提示 + Full 占位消息；`_current_lane=="full"` |
| 5 | `test_handle_forced_fast_skips_route` | `forced_lane="fast"`；`_route` 未被调用；events 含"强制车道: fast" + send 的事件 |
| 6 | `test_handle_forced_full_overrides_lane` | `forced_lane="full"`；`_route` 被调用（拆任务），lane 覆盖为 full；events 含"强制车道: full" + 占位消息 |
| 7 | `test_handle_route_exception_degrades_to_fast` | mock `_route` raise；events 含降级提示 + send 的事件；`_current_lane=="fast"` |
| 8 | `test_lane_guard_fast_allows_business_write` | `_current_lane="fast"`；`_lane_guard("Write",{},None)` → `PermissionResultAllow` |
| 9 | `test_lane_guard_full_denies_business_write` | `_current_lane="full"`；`_lane_guard("Write",{},None)` → `PermissionResultDeny` |
| 10 | `test_lane_guard_none_denies_business_write` | `_current_lane=None`；`_lane_guard("Edit",{},None)` → `PermissionResultDeny` |
| 11 | `test_lane_guard_allows_bash_any_lane` | `_current_lane="full"`；`_lane_guard("Bash",{},None)` → `PermissionResultAllow` |
| 12 | `test_lane_guard_allows_read_any_lane` | `_current_lane="full"`；`_lane_guard("Read",{},None)` → `PermissionResultAllow` |
| 13 | `test_entrance_resets_lane` | 连续两次 `handle_user_input`：第二次入口（自动路由）将 `_current_lane` 重置为 None（路由判定前） |
| 14 | `test_build_resident_session_config` | `router._session.permission_mode=="default"`；`can_use_tool is not None`；`"Write" not in allowed_tools`；`"Bash" in allowed_tools`；`system_prompt==EXECUTOR_PROMPT` |
| 15 | `test_session_property` | `router.session is router._session` |
| 16 | `test_rebuild_creates_new_session` | mock start/close；rebuild 后 `router._session` 是新实例（旧 `id` 不同） |
| 17 | `test_route_uses_query_top_level_api` | mock query；`_route()` 调用后断言 query 被调用，且 `opts.tools==[]`、`opts.output_format` 非空、`opts.permission_mode` 未设（路由判定无权限约束） |

> **mock query 写法**：`query` 是关键字参数函数 `query(*, prompt, options=None)`。fake 实现：
> ```python
> async def fake_query(*, prompt, options=None):
>     yield FakeResultMessage(structured_output={"lane":"fast","reason":"r","tasks":[]})
> ```
> 用 `types.SimpleNamespace` 或自定义 dataclass 模拟 `ResultMessage`（带 `structured_output` 属性）。

---

## 4. 任务分解与执行

> 沿用 Step 1 工作流：派 general-purpose subagent 执行（custom code-reviewer 在本环境不可用 — 见 memory `custom-agents-unspawnable`），每个 subagent 完成后自审：读 `.claude/agents/code-review-agent.md` 并应用其 checklist。

### Task A：实现 architect.py + utils.py + __init__.py 导出
- 新建 `src/harness_agent/core/utils.py`（§2.1）
- 新建 `src/harness_agent/core/architect.py`（§2.2，含 ROUTE_JUDGE_PROMPT/EXECUTOR_PROMPT 全文 — 取自架构师.md §3/§5）
- 更新 `src/harness_agent/core/__init__.py`（§2.3）
- 自审：读 code-review-agent.md，按 checklist 审 architect.py + utils.py
- 验证：`ruff check` 三文件 + `python -c "from harness_agent.core import L0Router"` 导入无错

### Task B：测试 test_l0_router.py + test_utils.py
- 依赖 Task A 的接口契约（§2）— 可与 Task A 并行（契约已锁定）
- 新建 `tests/test_utils.py`（§3 顶部）
- 新建 `tests/test_l0_router.py`（§3 表格 17 测试点）
- 自审：读 code-review-agent.md，按 checklist 审测试（mock 正确性、断言精度、无真实 SDK 调用）
- 验证：`PYTHONPATH=src python -m pytest tests/test_l0_router.py tests/test_utils.py -q` 全过

### 执行顺序
Task A + Task B **并行**派发（接口契约已精确锁定，测试按契约写）。两 Task 都完成后我统一跑 `ruff` + 全量 `pytest` 验证，处理交叉问题。

---

## 5. 风险与边界

| # | 风险 | 应对 |
|---|---|---|
| 1 | `_lane_guard` 忘记 `async` → SDK CanUseTool 期望 Awaitable | 契约明确 `async def`；测试 8-12 `await _lane_guard(...)` 验证返回 PermissionResult（非 coroutine） |
| 2 | `query()` 是关键字参数函数 → 误用位置参数 | 契约明确 `query(prompt=text, options=opts)`；测试 17 验证调用 |
| 3 | `_route` mock 点错误（patch 不到） | patch `harness_agent.core.architect.query`（architect.py 顶部导入的 `query` 符号） |
| 4 | `handle_user_input` 是 async generator → 测试需 `async for` 收集 events | 测试用 `async def` + `async for` 收集到 list |
| 5 | `_build_resident_session` 在 `__init__` 调用 → 构造 L0Router 即创建 BaseAgentSession（不 start，安全） | BaseAgentSession.__init__ 不连接 SDK，仅设属性；测试可直接检查 `router._session` 配置 |
| 6 | `_run_full` 占位被误当真实实现 | 占位消息明确标注"Step 4 实现"；测试 4 验证占位消息内容 |
| 7 | Step 2 引入 Step 4/5 字段（死代码） | 严格只建 `_current_lane`/`project_dir`/`model`/`_session`/`_session_store`；reviewer 审查死代码 |
| 8 | `forced_lane="full"` 但 `_route` 失败 → 降级 fast | 测试 7 覆盖（forced full 路由失败也降级 fast，契约一致） |

---

## 6. 验收清单（Step 2 范围）

- [ ] `core/architect.py` 实现 L0Router（路由判定 + Fast Lane + 降级 + _lane_guard + rebuild + session property + start/close + _run_full 占位）
- [ ] `core/utils.py` 实现 `_build_message`
- [ ] `core/__init__.py` 导出 `L0Router`
- [ ] `_route` 走 `query()` 顶层 API + `output_format`，`tools=[]`，不新建 BaseAgentSession，不持久化
- [ ] `_lane_guard` async，fast 放行 / full/None 禁业务写 / Bash 始终放行
- [ ] 常驻 session：`permission_mode="default"` + 业务写不进 `allowed_tools` + `can_use_tool=_lane_guard`
- [ ] `handle_user_input` 返回 `AsyncIterator[ChatEvent]`，支持 forced_lane 三态
- [ ] 路由降级：`_route` 异常 → 降级 Fast + 提示
- [ ] 入口重置：`_current_lane` 在自动/forced full 入口重置为 None
- [ ] 无 Step 4/5 字段残留（`_total_cost`/`_worktree_registry`/`CLI_TIMEOUT`/`_full_active` 等不出现）
- [ ] ruff clean（architect.py + utils.py + __init__.py）
- [ ] `tests/test_l0_router.py` 17 测试 + `tests/test_utils.py` 2 测试全过
- [ ] 全量 pytest 不引入新失败（预存在的 test_smoke 版本号失败除外）
- [ ] architect.py 核心逻辑 ≤ ~200 行（Step 2 范围，Full 实现后 Step 4 达 ~250 行）
