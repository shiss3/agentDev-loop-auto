"""Governor — L0 治理者（Phase 2 Step 3）。

职责：
1. 需求解析：独立 stateless query() + output_format，输出标准化任务单，不进常驻 session 历史
2. 双轨调度：纯规则判定交互轨 / 交付轨（任务规模 + 上下文连续性），不调 LLM
3. 交互轨直执行：经 self._resident.send() 产出 ChatEvent 流（常驻执行体，上下文连续）
4. 解析异常降级：_parse_requirement 异常 → 降级交互轨直接执行，不阻断用户

核心铁则：L0 绝不下场编码。判定与执行物理分离：
- 判定 session：stateless query()，tools=[]（只读，不挂业务写工具）
- 交互轨执行体：常驻 BaseAgentSession（可写，复用上下文）
- 交付轨执行体：独立 BaseAgentSession 实例 + worktree cwd（后续层实装）
不再有 lane guard —— 执行体本身可写，判定 session 本身不挂业务写工具。

交付轨灌队列本步实装；执行器 spawn/worktree/验收/修复闭环属后续层。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SessionStore,
    query,
)

from harness_agent.chat.events import ChatEvent
from harness_agent.chat.session_store import create_session_store
from harness_agent.core.base_session import BaseAgentSession
from harness_agent.core.task_queue_adapter import TaskQueueAdapter
from harness_agent.core.utils import _build_message

# ── 常量 ──────────────────────────────────────────────────────────────

# 标准化任务单 schema —— 替换原 ROUTER_DECISION_SCHEMA（按技术领域拆任务的产物）
TASK_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "task_summary": {"type": "string"},  # 一句话核心目标
        "acceptance_criteria": {  # 结构化验收要点（可验证）
            "type": "array",
            "items": {"type": "string"},
        },
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "suggest_track": {
            "type": "string",
            "enum": ["interactive", "delivery"],
        },
        "expected_scope": {"type": "string"},  # 预估影响范围（仅调度参考）
        "subtasks": {  # 仅交付轨复杂需求：按技术领域 + 业务功能单元拆
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},  # 本批内唯一短 id（如 "t1"），供 deps 引用
                    "domain": {"type": "string"},  # 技术领域：frontend/backend/database/docs/...
                    "summary": {"type": "string"},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "intended_files": {  # 预期改动文件（可空）
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "deps": {  # 依赖的同批 subtask id 列表（无依赖留空数组）
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "scope_hint": {"type": "string"},  # 影响文件/模块提示，worktree 隔离用
                },
                "required": ["id", "domain", "summary", "acceptance"],
            },
        },
    },
    "required": ["task_summary", "acceptance_criteria", "risk_level", "suggest_track"],
}

REQUIREMENT_PARSER_PROMPT = """\
# 角色：L0 需求解析员

你只做一件事：把用户需求转化为结构化、可验证的标准任务单。你不执行需求，不指导实现细节。

## 输出字段
- task_summary：一句话概括任务核心目标。
- acceptance_criteria：结构化验收要点列表，每项必须可验证（能跑命令/测试/接口验证），
  锚定原始需求，禁止自行增减范围。
- risk_level：低/中/高，用于调度与验收等级匹配。
- suggest_track：interactive（单模块局部改动）/ delivery（多模块/多领域完整需求）。
- expected_scope：预估影响的业务范围与功能模块（仅调度参考）。
- subtasks：仅当 suggest_track=delivery 时，按【技术领域 + 业务功能单元】拆分；每项：
  - id：本批内唯一短 id（"t1","t2",...），供 deps 引用。
  - domain：技术领域（frontend/backend/database/docs/...）。
  - summary：该子任务核心目标。
  - acceptance：该子任务可验证的验收要点。
  - intended_files：预期改动的文件路径列表（不确定可留空）。
  - deps：依赖的同批 subtask id 列表（必须先完成的；无依赖留空数组）。
  单模块任务留空数组。

## 纪律
- 严格锚定原始需求，所有验收项可追溯到需求原文。
- subtasks 的 prompt/summary 必须自包含（执行器看不到你的上下文）。
- 依赖按真实硬依赖填（"先建表再写 API"），软偏好不填。
- 仅做语义层面的歧义补全，遵循项目通用规范与行业默认最佳实践；补全决策在 task_summary
  或 acceptance_criteria 中体现可追溯性。
- 模糊时 suggest_track 取 interactive（开销小，宁可走交互轨）。
- 不指导实现方案、不指定技术栈、不替模型做执行层决策。
"""

EXECUTOR_PROMPT = """\
# 角色：执行层（由 L0 治理层派发任务）

你是代码实现的执行者。L0 治理层只下发任务目标与验收标准，不干预你的实现路径。

## 你的自主权
- 自主规划实现步骤、安排开发顺序。
- 自主读代码、编辑文件、执行命令。
- 按需派生 subagent 处理子任务、做代码评审。

## 你的契约
- 你的输出是「待验收半成品」，不自行宣告任务完成 —— L0 会用客观工具验收。
- 严格对齐 L0 下发的验收要求，不修改验收标准。
- 遇到需求模糊先澄清，不要擅自扩大或缩小范围。
"""


# ── Governor ──────────────────────────────────────────────────────────


class Governor:
    """L0 治理者 — 需求解析 + 双轨调度。验收/修复闭环 Step 4-6 接入。

    - 需求解析走 query() 顶层 API（stateless，不进常驻执行体历史，不持久化）
    - 交互轨经 self._resident.send(text) 产出 ChatEvent 流（text 首次进常驻执行体）
    - 交付轨灌队列本步实装，执行器属后续层
    - 判定与执行物理分离：执行体 permission_mode="acceptEdits" + 业务写进 allowed_tools，
      不挂 can_use_tool；判定 session tools=[] 天然只读。无需 lane guard。
    """

    def __init__(
        self,
        project_dir: str,
        model: str | None = None,
        *,
        session_store: SessionStore | None = None,
        task_queue: TaskQueueAdapter | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.model = model
        self._session_store = (
            session_store if session_store is not None else create_session_store()
        )
        self._resident = self._build_resident_session()
        self._task_queue = (
            task_queue if task_queue is not None else self._build_task_queue()
        )
        self._last_text: str | None = None

    @property
    def session(self) -> BaseAgentSession:
        """穿透到常驻执行体（commands.py 的 cli.session.* 零改动，Step 6 用）。"""
        return self._resident

    def _build_resident_session(self) -> BaseAgentSession:
        """构造交互轨常驻执行体（__init__ 与 rebuild 共用）。

        执行层配置（非 L0 判定 session）：
        - system_prompt=EXECUTOR_PROMPT（极简执行契约，不写领域指导）
        - permission_mode="acceptEdits"（执行体可写）
        - allowed_tools=["Read","Write","Edit","Bash","Glob","Grep"]（业务写进白名单）
        - 不挂 can_use_tool（判定与执行分离，无需 lane guard）
        - session_store=self._session_store（持久化，跨进程可 resume）
        """
        return BaseAgentSession(
            project_dir=self.project_dir,
            model=self.model,
            system_prompt=EXECUTOR_PROMPT,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            permission_mode="acceptEdits",
            session_store=self._session_store,
        )

    async def start(self) -> None:
        await self._resident.start()

    async def close(self) -> None:
        await self._resident.close()

    async def rebuild(
        self, *, project_dir: str | None = None, model: str | None = None
    ) -> None:
        """重建常驻执行体（switch_project / switch_model / reset_session 调用，Step 6 用）。"""
        if project_dir is not None:
            self.project_dir = project_dir
        if model is not None:
            self.model = model
        await self._resident.close()
        self._resident = self._build_resident_session()
        await self._resident.start()

    @staticmethod
    def _new_req_id() -> str:
        """生成 8 字符 hex req_id（uuid4 截断）。"""
        return uuid.uuid4().hex[:8]

    @staticmethod
    def _build_task_queue() -> TaskQueueAdapter | None:
        """从环境变量构造 adapter；未配置返回 None（交付轨降级）。"""
        svc_dir = os.environ.get("TASK_SERVICE_DIR")
        db_path = os.environ.get("TASK_DB")
        if not svc_dir or not db_path:
            return None
        try:
            return TaskQueueAdapter(svc_dir, db_path)
        except Exception:
            return None

    @staticmethod
    def _build_task_prompt(subtask: dict, spec: dict) -> str:
        """拼自包含任务 prompt（执行器看不到解析上下文）。"""
        acc = "\n".join(f"- {a}" for a in subtask.get("acceptance", []))
        files = subtask.get("intended_files") or []
        files_line = f"\n预期文件: {', '.join(files)}" if files else ""
        return (
            f"# 任务: {subtask['summary']}\n"
            f"## 需求背景\n{spec.get('task_summary', '')}\n"
            f"## 验收标准\n{acc}{files_line}"
        )

    async def _parse_requirement(
        self, text: str, *, context_continuation: bool
    ) -> dict:
        """独立 stateless 需求解析。query() 顶层 API + output_format，不新建 BaseAgentSession。

        - tools=[]（纯解析，不调工具，不写代码）
        - max_turns=3
        - 不挂 session_store（一次性，用完即弃）
        - 不挂 can_use_tool（判定无工具调用）
        返回 ResultMessage.structured_output（dict）。未返回 → RuntimeError（被 handle_user_input 捕获降级）。
        """
        opts = ClaudeAgentOptions(
            system_prompt=REQUIREMENT_PARSER_PROMPT,
            tools=[],
            output_format={"type": "json_schema", "schema": TASK_SPEC_SCHEMA},
            cwd=self.project_dir,
            model=self.model,
            max_turns=3,
        )
        prefix = "接续修改" if context_continuation else "全新任务"
        prompt = f"[{prefix}] {text}"
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, ResultMessage) and msg.structured_output:
                return msg.structured_output
        raise RuntimeError("需求解析未返回结构化结果")

    def _decide_track(self, spec: dict, *, context_continuation: bool) -> str:
        """双轨调度判定 —— 纯规则，不调 LLM。

        规则（对齐 governance_plan §4.3）：
        - spec.subtasks 非空 → delivery（多模块）
        - 否则取 spec.suggest_track（缺省 interactive）
        context_continuation 当前仅记录，Step 6 接入 REPL 后用于「接续 → 升级 delivery」判定。
        """
        if spec.get("subtasks"):
            return "delivery"
        return spec.get("suggest_track", "interactive")

    async def handle_user_input(
        self, text: str, *, forced_track: str | None = None
    ) -> AsyncIterator[ChatEvent]:
        """用户输入 → ChatEvent 流。

        forced_track 语义：
          None             → 自动判定（_parse_requirement + _decide_track）
          "interactive"    → 跳过解析，直接交互轨执行
          "delivery"       → 跳过解析，直接交付轨
        """
        self._last_text = text
        context_continuation = self._resident.stats.turn_count > 0

        if forced_track:
            track = forced_track
            spec = {
                "task_summary": text,
                "acceptance_criteria": [],
                "risk_level": "medium",
                "suggest_track": track,
            }
        else:
            try:
                spec = await self._parse_requirement(
                    text, context_continuation=context_continuation
                )
            except Exception as e:
                yield _build_message(
                    f"⚠️ 需求解析异常({str(e)[:80]}),已降级为交互轨直接执行。"
                )
                async for event in self._resident.send(text):
                    yield event
                return
            track = self._decide_track(spec, context_continuation=context_continuation)

        yield _build_message(
            f"🔀 调度: {track}（{spec.get('task_summary', '')[:40]}）"
        )

        if track == "interactive":
            async for event in self._resident.send(text):
                yield event
            return

        # track == "delivery"
        async for event in self._run_delivery(spec):
            yield event

    async def _run_delivery(self, spec: dict) -> AsyncIterator[ChatEvent]:
        """交付轨：subtasks -> task_queue tasks -> seed 灌队列 -> 返回 req_id。

        纯解耦：灌完即返回，不 spawn 执行器。
        """
        subtasks = spec.get("subtasks") or []
        if not subtasks:
            yield _build_message("⚠️ 交付轨无子任务，降级交互轨执行。")
            async for event in self._resident.send(self._last_text):
                yield event
            return

        if self._task_queue is None:
            yield _build_message("⚠️ 未配置任务队列，降级交互轨执行。")
            async for event in self._resident.send(self._last_text):
                yield event
            return

        req_id = self._new_req_id()
        id_map = {st["id"]: f"{req_id}-{st['id']}" for st in subtasks}

        tasks = []
        for st in subtasks:
            tasks.append({
                "id": id_map[st["id"]],
                "req_id": req_id,
                "domain": st.get("domain", "default"),
                "prompt": self._build_task_prompt(st, spec),
                "intended_files": st.get("intended_files") or [],
                "deps": [id_map[d] for d in st.get("deps", []) if d in id_map],
            })

        n = self._task_queue.seed(tasks)
        yield _build_message(
            f"🌐 交付轨 req_id={req_id}：已灌入 {n} 个任务到队列"
        )
