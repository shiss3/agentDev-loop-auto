"""L0 Router — 星射线入口路由器（Phase 2 Step 2）。

职责：
1. 路由判定：独立 stateless query() + output_format，不进常驻 session 历史
2. Fast Lane 自执行：经 self._session.send() 产出 ChatEvent 流
3. 动态权限硬约束：_lane_guard 按 lane 放行/拒绝业务写
4. 路由降级：_route 异常 → 降级 Fast Lane，不阻断用户

Full 模式（CLI 子进程 + worktree）Step 4 实现，本步占位。
"""

from __future__ import annotations

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

# ── 常量 ──────────────────────────────────────────────────────────────

BUSINESS_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
# Bash / Read / Glob / Grep 始终放行（运维 + 只读）

ROUTER_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["fast", "full"]},
        "reason": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "prompt": {"type": "string"},
                    "intended_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["domain", "prompt"],
            },
        },
    },
    "required": ["lane", "reason", "tasks"],
}

ROUTE_JUDGE_PROMPT = """\
# 角色:星射线入口路由器(判定员)

你只做一件事:判断用户需求是单领域还是多领域,输出结构化结果。你不执行需求。

## 判定规则
- 单领域(fast):只涉及一个技术领域(纯前端改色 / 纯后端加接口 / 纯数据库建表)。
- 多领域(full):跨多个领域(如"重构完整支付链路"涉及前端+后端+数据库)
  → 拆出每个领域的独立提示词。

## 纪律
- 领域数量按需求实质判断,不被关键词数量误导。
  例:"把导航栏颜色改成蓝色" = fast,即使出现"导航/颜色/蓝色"多个词。
- 模糊时 lane 判 fast(fast 开销小,宁可走 fast),并在 reason 标注不确定。
- full 时每个 task 的 prompt 必须自包含(①需求 ②边界 ③验收),子进程看不到你的上下文。
"""

EXECUTOR_PROMPT = """\
# 角色:星射线执行体(L0 Executor)

你直接处理用户的开发需求。你有完整的文件读写与命令执行能力。
读项目 → 理解现状 → 修改 → 自验证 → 完成。遇到模糊处先澄清。
"""


# ── L0Router ──────────────────────────────────────────────────────────


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
        self._current_lane: str | None = None
        self.project_dir = project_dir
        self.model = model
        self._session_store = (
            session_store if session_store is not None else create_session_store()
        )
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
        - session_store=self._session_store
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

        - fast → Allow（业务写放行）
        - full/None（异常残留）→ Deny 业务写
        - 非业务写工具（Bash/Read/...）一律 Allow
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
        - max_turns=3
        - 不挂 session_store（一次性，用完即弃）
        - 不挂 can_use_tool（路由判定无工具调用）
        返回 ResultMessage.structured_output。未返回 → RuntimeError（被 handle_user_input 捕获降级）。
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

        forced_lane 语义：
          None    → 自动路由（_route 判定 lane + 拆任务）
          "fast"  → 跳过 _route，直接 Fast Lane 执行
          "full"  → 仍调 _route 拆域任务，但强制 lane="full"
        """
        # 1. 入口设定 lane
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
                decision["lane"] = "full"

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
