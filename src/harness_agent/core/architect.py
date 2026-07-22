"""Governor — L0 治理者（Phase 2 Step 3）。
职责：
1. 需求解析：独立 stateless query() + submit_analysis_plan tool（带项目探索），输出标准化任务单，不进常驻 session 历史
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
import asyncio
import datetime
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SessionStore,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from harness_agent.chat.events import ChatEvent
from harness_agent.chat.session_store import create_session_store
from harness_agent.core.base_session import BaseAgentSession
from harness_agent.core.executor import (
    build_dispatch_manifest,
    build_executor_args,
    build_loop_prompt,
    spawn_executor,
    write_mcp_config,
)
from harness_agent.core.module_affinity import normalize_module_id, sanitize_module_id
from harness_agent.core.task_queue_adapter import TaskQueueAdapter
from harness_agent.core.utils import _build_message
from harness_agent.core.worktree import (
    commit_worktree,
    create_delivery_worktree,
    merge_worktree_branch,
    remove_worktree,
)
# ── 常量 ──────────────────────────────────────────────────────────────
# 标准化任务单 schema —— 替换原 ROUTER_DECISION_SCHEMA（按技术领域拆任务的产物）
TASK_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "request_kind": {  # 输入分类(_decide_track 门消费):dev_task/meta_request/question/other
            "type": "string",
            "enum": ["dev_task", "meta_request", "question", "other"],
        },
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
        "change_type": {  # 客观字段：修补 / 功能交付（_decide_track 消费）
            "type": "string",
            "enum": ["patch", "feature"],
        },
        "file_count_bucket": {  # 客观字段：预估改动文件档（_decide_track 消费）
            "type": "string",
            "enum": ["1-5", "6+"],
        },
        "cross_domain": {  # 客观字段：跨领域/改公共契约（_decide_track 消费）
            "type": "boolean",
        },
        "subtasks": {  # 复杂需求按技术领域+业务模块拆（A 方案：feature/6+/cross_domain 触发）
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},  # 本批内唯一短 id（如 "t1"），供 deps 引用
                    "domain": {"type": "string", "enum": ["frontend", "backend", "database", "docs", "infra", "test"]},  # 技术领域枚举
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
                    "module_id": {"type": "string"},  # 业务模块英文 slug（小写+下划线）
                    "scope_hint": {"type": "string"},  # 影响文件/模块提示，worktree 隔离用
                },
                "required": ["id", "domain", "summary", "acceptance", "module_id"],
            },
        },
    },
    "required": [
        "request_kind",
        "task_summary",
        "acceptance_criteria",
        "risk_level",
        "suggest_track",
        "change_type",
        "file_count_bucket",
        "cross_domain",
    ],
}
REQUIREMENT_PARSER_PROMPT = """\
# 角色：L0 需求解析员
你只做一件事：把用户需求转化为结构化、可验证的标准任务单。你不执行需求，不指导实现细节。
## 工作流程（不盲拆）
0. 先判 request_kind（分类约定见下"request_kind 分类"）：meta_request/question 不探索代码、不拆 subtasks，直接跳第 2 步。
1. 探索项目（仅 dev_task）：用 codegraph_explore 查符号/文件/调用路径，用 Read/Grep/Glob 读相关模块。
   先看代码再拆任务，intended_files/scope_hint 才有依据。不探索直接拆=盲拆，禁止。
2. 调用 submit_analysis_plan 提交结构化任务单（参数=任务单各字段）。
## 输出字段
- task_summary：一句话概括任务核心目标。
- acceptance_criteria：结构化验收要点列表，每项必须可验证（能跑命令/测试/接口验证），
  锚定原始需求，禁止自行增减范围。
- risk_level：low/medium/high。high=高风险（改支付/迁移/删数据/安全敏感），强制走 delivery
  让下游验收/隔离；常驻 acceptEdits 不直写高风险改动。
- change_type：客观判定改动性质。patch=修修补补（改文案/调样式/加字段/改参数/小 bug）；
  feature=完整功能交付。
- file_count_bucket：预估改动文件数分档。1-5=少量文件；6+=多文件。不猜精确数，只分档。
- cross_domain：是否跨技术领域（前后端/多模块）或改公共契约（新增接口/改数据结构/改公共依赖）。
  true=是；false=否。
- suggest_track：interactive / delivery（LLM 主观参考，不作 track 依据；track 由代码规则定）。
- expected_scope：预估影响的业务范围与功能模块（仅调度参考）。
- subtasks：按【技术领域 + 业务功能单元】拆分。拆分规则（A 方案）：
  当 change_type=feature 或 file_count_bucket=6+ 或 cross_domain=true 或 risk_level=high 时**必须拆 subtasks**；
  否则不拆（subtasks 为空数组）。high 小补丁也拆（整个补丁当 1 个 task 灌队列，保 delivery 隔离不降级常驻）。
  粒度软约束：每 subtask 聚焦 1-5 文件，整批 1-8 个（high 小补丁可仅 1 个，豁免下限）
  （太细膨胀队列+deps 网，太粗失聚拢；超范围按真实硬依赖切分）。每项：
  - id：本批内唯一短 id（"t1","t2",...），供 deps 引用。
  - domain：技术领域枚举 frontend/backend/database/docs/infra/test，必选其一。
  - module_id：业务大模块的英文 slug（小写+下划线，如 payment/user_auth/session）。
    同一需求内同一模块用同一 slug；一个模块可含多领域（前端+后端+数据库）。
  - summary：该子任务核心目标。
  - acceptance：该子任务可验证的验收要点。
  - intended_files：预期改动的文件路径列表（探索后填，不确定可留空）。
  - deps：依赖的同批 subtask id 列表（必须先完成的；无依赖留空数组）。
  每项标 module_id（业务模块）+ domain（技术领域）；同 (module_id, domain) 可多 subtask。
  执行器按 (module_id, domain) 聚合领取以减上下文噪音，故 module_id/domain 须规范一致。
## 调用指引
- 分析完成后必须调用 submit_analysis_plan 提交任务单，不要只输出文本。
- submit_analysis_plan 参数 = 任务单各字段（task_summary/acceptance_criteria/.../subtasks），
  schema 与上述字段约定一致。
- 调用后工具返回确认字符串，解析结束，不要再输出其他内容。
## 纪律
- 严格锚定原始需求，所有验收项可追溯到需求原文。
- subtasks 的 prompt/summary 必须自包含（执行器看不到你的上下文）。
- 依赖按真实硬依赖填（"先建表再写 API"），软偏好不填。
- 仅做语义层面的歧义补全，遵循项目通用规范与行业默认最佳实践；补全决策在 task_summary
  或 acceptance_criteria 中体现可追溯性。
- 模糊时取保守（change_type 倾向 feature、file_count_bucket 倾向 6+、cross_domain 倾向 true、
  risk_level 倾向 high，即倾向 delivery）。
- track 由代码规则消费 risk_level/change_type/file_count_bucket/cross_domain 判定，suggest_track 仅供参考。
- 不指导实现方案、不指定技术栈、不替模型做执行层决策。
## request_kind 分类（先判类型再拆）
- dev_task：改代码/加功能/修 bug 的开发任务 -> 走完整流程拆 subtasks。
- meta_request：要你产出文本（生成需求/文档/方案/示例）而非改代码 -> subtasks=[]，risk=low，不探索代码。
- question：问答/解释/分析 -> subtasks=[]，risk=low。
- other：确非前三类且无法归类才用，罕用；不确定默认归 dev_task（勿用 other 逃逸分类）。
meta_request/question 不标 high（无代码改动无高风险）。
## 非开发需求字段约定（meta_request/question）
不要拒绝、不要追问、不要留空字段，直接按 schema 调用 submit_analysis_plan：
- task_summary：摘要用户原话或产出目标（如"生成一个能触发交付轨的需求"）
- acceptance_criteria：[]（空数组，无可验收项）
- risk_level：low
- change_type：patch
- file_count_bucket：1-5
- cross_domain：false
- suggest_track：interactive
- subtasks：[]（空数组）
调度层据 request_kind 门判为 interactive 走常驻交互。必填字段禁止留空、禁止输出 schema 外内容。
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
## 元任务处理（request_kind=meta_request/question）
L0 可能下发非开发任务（生成需求/文档/方案/示例/问答/分析）。此类按字面产出文本，
不当开发任务执行、不改业务代码；仅当任务明确要产出文件（如写文档）才创建对应文件。
"""
# 明显非需求输入(寒暄/确认/道别)白名单 -- handle_user_input 直接走交互轨跳过 LLM 解析省 token。
# 仅抓最无歧义词;真任务(含动词/文件名/需求描述)不在此列,走 _parse_requirement。
# 采纳确认白名单（resident_plan 闭环）：用户回复这些短语时消费 _pending_plan 灌队列。
# 与寒暄一样走归一化精确匹配，避免误吞真任务文本。
_ADOPTION_INPUTS = frozenset({
    "采用方案", "采纳方案", "采用该方案", "采纳该方案",
    "按方案执行", "确认采用", "确认采纳",
})
_CASUAL_INPUTS = frozenset({
    "你好", "您好", "嗨", "在吗", "在不在",
    "谢谢", "感谢", "收到", "明白", "了解", "好的", "嗯", "哦", "ok",
    "早", "早上好", "下午好", "晚上好",
    "再见", "拜拜", "hi", "hello", "hey", "thanks", "thx", "bye",
})
async def _default_can_use_tool(
    tool_name: str, tool_input: dict, context: ToolPermissionContext
) -> PermissionResult:
    """常驻执行体默认权限回调：非 AskUserQuestion 直接放行，无用户交互。
    AskUserQuestion 默认拒绝（TUI 问答卡由 chat 层注入回调处理；未注入时无法向用户提问）。
    """
    if tool_name == "AskUserQuestion":
        return PermissionResultDeny(
            message="当前会话未接入交互问答通道，无法向用户提问"
        )
    return PermissionResultAllow()
# ── Governor ──────────────────────────────────────────────────────────
class Governor:
    """L0 治理者 — 需求解析 + 双轨调度。验收/修复闭环 Step 4-6 接入。
    - 需求解析走 query() 顶层 API（stateless，不进常驻执行体历史，不持久化）
    - 交互轨经 self._resident.send(text) 产出 ChatEvent 流（text 首次进常驻执行体）
    - 交付轨灌队列本步实装，执行器属后续层
    - 判定与执行物理分离：执行体 permission_mode="default" + can_use_tool 回调
      （ask 态必触发回调：非 AskUserQuestion 直接放行，chat 层可注入自定义回调处理
      AskUserQuestion；bypassPermissions 下回调不触发，故不用）；判定 session
      tools=[] 天然只读。无需 lane guard。
    """
    def __init__(
        self,
        project_dir: str,
        model: str | None = None,
        *,
        session_store: SessionStore | None = None,
        task_queue: TaskQueueAdapter | None = None,
        can_use_tool: CanUseTool | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.model = model
        self._session_store = (
            session_store if session_store is not None else create_session_store()
        )
        # chat 层注入的权限回调（None 时用默认：非 AskUserQuestion 直接放行）。
        # rebuild 经 _build_resident_session 复读本字段，重建后仍生效。
        self._can_use_tool = (
            can_use_tool if can_use_tool is not None else _default_can_use_tool
        )
        # resident_plan：常驻会话方案生成工具截获的待采纳方案（Governor 持有，rebuild 不丢）。
        # 用户回复"采用方案"时消费（灌 task.db，后续任务实装）。
        self._pending_plan: dict | None = None
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
        - permission_mode="default"（ask 态走 can_use_tool 回调；可用工具集不裁剪，
          原生+自定义+MCP+skills 全可用）
        - allowed_tools=[]（不预放行任何工具，全部经回调/CLI 规则判定）
        - can_use_tool=self._can_use_tool（chat 层可注入；默认非 AskUserQuestion 直接放行）
        - session_store=self._session_store（持久化，跨进程可 resume）
        - mcp_servers={"plan_gen": 方案生成 SDK server}（resident_plan：input_schema 复用
          TASK_SPEC_SCHEMA，输出与计划格式一致；与 task_queue 配置无关，恒挂载）
        - tool_intercept=self._on_tool_use（流式 tool_use 兜底，参照 _parse_requirement 模式）
        __init__ 与 rebuild 共用本方法 -> 每次重建重新建 server，工具恒挂载。
        """
        return BaseAgentSession(
            project_dir=self.project_dir,
            model=self.model,
            system_prompt=EXECUTOR_PROMPT,
            allowed_tools=[],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            session_store=self._session_store,
            mcp_servers={"plan_gen": self._build_plan_server()},
            tool_intercept=self._on_tool_use,
        )
    # ── resident_plan：方案生成工具（常驻会话挂载）──
    def _build_plan_server(self):
        """构造方案生成 SDK MCP server（plan_gen.propose_plan）。

        input_schema 复用 TASK_SPEC_SCHEMA -> 工具输出与计划格式一致。
        handler 截获 args 存 self._pending_plan（待采纳方案状态）。
        """
        propose_tool = tool(
            name="propose_plan",
            description=(
                "生成实施方案（参数=任务单各字段，与标准化任务单 schema 一致）。"
                "产出实施计划时调用此工具；方案被记录为待采纳，用户回复\"采用方案\"后灌入交付队列。"
            ),
            input_schema=TASK_SPEC_SCHEMA,
        )(self._capture_plan)
        return create_sdk_mcp_server(name="plan_gen", tools=[propose_tool])
    async def _capture_plan(self, args: dict) -> dict:
        """propose_plan handler：截获工具参数为待采纳方案（主通道）。"""
        self._pending_plan = args
        return {"content": [{"type": "text", "text": "方案已记录为待采纳，等待用户确认"}]}
    def _on_tool_use(self, tool_name: str, tool_input: dict) -> None:
        """常驻会话流式 tool_use 兜底（handler 未触发时从 raw block 补获，参照 _parse_requirement）。"""
        if tool_name == "mcp__plan_gen__propose_plan" and self._pending_plan is None:
            self._pending_plan = tool_input
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
        module_id = normalize_module_id(subtask.get("module_id", "default"))
        domain = subtask.get("domain", "default")
        return (
            f"# 任务: {subtask['summary']}\n"
            f"## 需求背景\n{spec.get('task_summary', '')}\n"
            f"## 模块/领域\n{module_id}/{domain}\n"
            f"## 验收标准\n{acc}{files_line}"
        )
    async def _parse_requirement(
        self, text: str, *, context_continuation: bool
    ) -> dict:
        """独立 stateless 需求解析（不盲拆 - 带项目探索能力）。
        - submit_analysis_plan SDK tool:模型探索完项目后调用此工具提交结构化任务单,
          handler 截获 args 存闭包变量;流 AssistantMessage.tool_use 兜底(handler 未触发时)。
        - mcp_servers:plan_capture(SDK MCP,装 submit_analysis_plan)+ codegraph(stdio,探索工具)。
        - allowed_tools:只放读工具(codegraph_explore/Read/Glob/Grep)+ submit_analysis_plan,
          disallowed_tools 禁一切写工具/任务工具/问答工具,保解析只读不副作用。
        - strict_mcp_config=True:保留 model/env,禁 settings.json 的外部 MCP server。
        - max_turns=40:探索+拆任务+调用工具需多轮,4 turn 不够。
        截获失败 -> RuntimeError(降级交互轨,handle_user_input :363 catch)。
        """
        captured_spec: dict | None = None

        async def _capture_handler(args: dict) -> dict:
            nonlocal captured_spec
            captured_spec = args
            return {"content": [{"type": "text", "text": "计划已接收，解析结束"}]}

        submit_tool = tool(
            name="submit_analysis_plan",
            description="提交结构化任务单(参数=任务单各字段)。分析完项目后调用此工具结束解析。",
            input_schema=TASK_SPEC_SCHEMA,
        )(_capture_handler)
        plan_server = create_sdk_mcp_server(name="plan_capture", tools=[submit_tool])

        opts = ClaudeAgentOptions(
            system_prompt=REQUIREMENT_PARSER_PROMPT,
            cwd=self.project_dir,
            model=self.model,
            max_turns=40,  # 探索+拆任务+调用工具需多轮
            strict_mcp_config=True,  # 禁外部 MCP(只留传入的 plan_capture/codegraph),保留 model/env
            mcp_servers={
                "plan_capture": plan_server,
                "codegraph": {
                    "type": "stdio",
                    "command": "codegraph",
                    "args": ["serve", "--mcp"],
                },
            },
            allowed_tools=[
                "mcp__codegraph__codegraph_explore",
                "Read", "Glob", "Grep",
                "mcp__plan_capture__submit_analysis_plan",
            ],
            disallowed_tools=[
                "Bash", "Edit", "Write", "WebSearch", "WebFetch",
                "TaskCreate", "TaskList", "TaskUpdate", "TaskGet",
                "EnterPlanMode", "ExitPlanMode", "AskUserQuestion",
            ],
        )
        prefix = "接续修改" if context_continuation else "全新任务"
        prompt = f"[{prefix}] {text}"
        in_tokens = out_tokens = tool_calls = 0
        _start = datetime.datetime.now()
        try:
            async for msg in query(prompt=prompt, options=opts):
                # 流兜底:handler 未触发时从 AssistantMessage 提取 tool_use input
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if not isinstance(block, ToolUseBlock):
                            continue
                        tool_calls += 1
                        if (block.name == "mcp__plan_capture__submit_analysis_plan"
                                and captured_spec is None):
                            captured_spec = block.input
                elif isinstance(msg, ResultMessage):
                    # ResultMessage.usage 是 dict(SDK 不平铺 input_tokens)
                    _usage = msg.usage or {}
                    in_tokens += _usage.get("input_tokens", 0) or 0
                    out_tokens += _usage.get("output_tokens", 0) or 0
        finally:
            # 自测日志(默认关,设 HARNESS_PARSE_LOG=1 开):解析阶段 in/out tokens + 工具调用数
            # -> .claude/parse-logs/usage.jsonl。finally 兜底:解析异常也写部分值供排查。
            if os.environ.get("HARNESS_PARSE_LOG"):
                _log_path = (
                    Path(self.project_dir) / ".claude" / "parse-logs" / "usage.jsonl"
                )
                _log_path.parent.mkdir(parents=True, exist_ok=True)
                _rec = {
                    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "duration_s": round(
                        (datetime.datetime.now() - _start).total_seconds(), 2
                    ),
                    "prefix": prefix,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "tool_calls": tool_calls,
                    "captured": captured_spec is not None,
                }
                with _log_path.open("a", encoding="utf-8") as _f:
                    _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
        if captured_spec is None:
            raise RuntimeError("需求解析未返回结构化结果")
        return captured_spec
    def _decide_track(self, spec: dict) -> str:
        """双轨调度判定 -- 纯规则消费 LLM 客观字段，不调 LLM。
        1. risk_level=high -> delivery（高风险安全网最先，防 high dev_task 被误标 meta/question 漏网）
        2. request_kind in (meta_request, question) -> interactive（元任务/问答不走交付轨）
           other 不门（按原规则判，避免成逃逸 interactive 的口）
        3. 交互轨(全满足且非 high)：change_type=patch AND file_count_bucket=1-5 AND cross_domain=False
        任一不满足或缺省 -> delivery（保守）
        context_continuation 不再影响 track（续聊也可能来复杂需求该走 delivery），
        仅作 _parse_requirement 的 prompt 前缀([接续修改]/[全新任务])。
        """
        if spec.get("risk_level") == "high":
            return "delivery"
        if spec.get("request_kind") in ("meta_request", "question"):
            return "interactive"
        if (spec.get("change_type") == "patch"
                and spec.get("file_count_bucket") == "1-5"
                and spec.get("cross_domain") is False):
            return "interactive"
        return "delivery"
    def _spec_to_tasks(self, spec: dict) -> tuple[str, list[dict]]:
        """spec.subtasks -> task_queue tasks（req_id 前缀 id + deps 映射）。
        交付轨（_run_delivery）与采纳闭环（_adopt_plan）共用。"""
        subtasks = spec.get("subtasks") or []
        req_id = self._new_req_id()
        id_map = {st["id"]: f"{req_id}-{st['id']}" for st in subtasks}
        tasks = []
        for st in subtasks:
            tasks.append({
                "id": id_map[st["id"]],
                "req_id": req_id,
                "domain": st.get("domain", "default"),
                "module_id": normalize_module_id(st.get("module_id", "default")),
                "prompt": self._build_task_prompt(st, spec),
                "intended_files": st.get("intended_files") or [],
                "deps": [id_map[d] for d in st.get("deps", []) if d in id_map],
            })
        return req_id, tasks
    @staticmethod
    def _is_casual_input(text: str) -> bool:
        """明显非需求输入(寒暄/确认/道别)直接走交互轨,跳过 LLM 解析省 token。
        去空白 + 去末尾标点 + 小写后精确匹配 _CASUAL_INPUTS 白名单。
        仅抓最无歧义的寒暄词;真任务(含动词/文件名/需求描述)不会被误判。
        """
        s = text.strip().rstrip("？！?!.。,，~~").lower()
        return s in _CASUAL_INPUTS
    @staticmethod
    def _is_adoption_input(text: str) -> bool:
        """采纳确认（"采用方案"等）—— 归一化精确匹配 _ADOPTION_INPUTS，不误吞真任务。"""
        s = text.strip().rstrip("？！?!.。,，~~").lower()
        return s in _ADOPTION_INPUTS
    async def _adopt_plan(self) -> AsyncIterator[ChatEvent]:
        """采纳闭环：待采纳方案 -> tasks -> seed 灌队列（供执行器 claim，不 spawn 执行器）。
        成功 seed 后清空 _pending_plan 防重复灌入；失败/降级路径保留方案可重试。"""
        plan = self._pending_plan
        if plan is None:
            yield _build_message(
                "⚠️ 无待采纳方案。请先在对话中让常驻执行体生成方案（propose_plan）。"
            )
            return
        if self._task_queue is None:
            yield _build_message(
                "⚠️ 未配置任务队列，方案无法灌入（方案保留，配置队列后可重试采纳）。"
            )
            return
        req_id, tasks = self._spec_to_tasks(plan)
        if not tasks:
            yield _build_message(
                "⚠️ 方案未拆出子任务，无法灌入队列（方案保留，可要求常驻执行体重新生成）。"
            )
            return
        try:
            n = self._task_queue.seed(tasks)
        except Exception as e:
            yield _build_message(
                f"⚠️ 灌队列失败({str(e)[:80]})，方案保留，可重试采纳。"
            )
            return
        self._pending_plan = None
        yield _build_message(
            f"✅ 方案已采纳 req_id={req_id}：已灌入 {n} 个任务到队列，供执行器 claim"
        )
    async def handle_user_input(
        self, text: str, *, forced_track: str | None = None
    ) -> AsyncIterator[ChatEvent]:
        """用户输入 → ChatEvent 流。
        forced_track 语义：
          None             → 自动判定（_parse_requirement + _decide_track）
          "interactive"    -> 跳过解析，直接交互轨执行（像寒暄直通，占位 spec）
          "delivery"       -> 解析保字段 + 强制交付轨（跳过 _decide_track；subtasks 空则拦截）
        """
        self._last_text = text
        context_continuation = self._resident.stats.turn_count > 0
        if self._is_adoption_input(text):
            # 采纳闭环：消费待采纳方案灌队列，优先于一切解析/调度
            async for event in self._adopt_plan():
                yield event
            return
        if forced_track == "interactive":
            # 场景1:不解析直通(像寒暄),占位 spec
            track = "interactive"
            spec = {
                "task_summary": text,
                "acceptance_criteria": [],
                "risk_level": "low",
                "suggest_track": "interactive",
            }
        elif forced_track == "delivery":
            # 场景2:解析保字段 -> 校验 subtasks -> 强制 delivery(跳过 _decide_track)
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
            if not (spec.get("subtasks") or []):
                yield _build_message(
                    "⚠️ 强制交付轨但解析未拆出子任务(输入可能非开发任务)。"
                    "请重述为开发需求,或用 /interactive 直通交互轨。"
                )
                return
            track = "delivery"
        else:
            if self._is_casual_input(text):
                # 寒暄/确认等非需求输入直接常驻交互,跳过 LLM 解析省 token
                track = "interactive"
                spec = {
                    "task_summary": text,
                    "acceptance_criteria": [],
                    "risk_level": "low",
                    "suggest_track": "interactive",
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
                track = self._decide_track(spec)
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
        is_high = spec.get("risk_level") == "high"
        if not subtasks:
            if is_high:
                yield _build_message(
                    "⚠️ 高风险需求但解析未拆出子任务（模型违规），已拦截不执行（常驻不直写高风险）。请人工介入或重述需求。"
                )
                return
            yield _build_message("⚠️ 交付轨无子任务，降级交互轨执行。")
            async for event in self._resident.send(self._last_text):
                yield event
            return
        if self._task_queue is None:
            if is_high:
                yield _build_message(
                    "⚠️ 高风险需求但未配置任务队列，已拦截不执行（常驻不直写高风险）。请配置队列或人工介入。"
                )
                return
            yield _build_message("⚠️ 未配置任务队列，降级交互轨执行。")
            async for event in self._resident.send(self._last_text):
                yield event
            return
        req_id, tasks = self._spec_to_tasks(spec)
        try:
            n = self._task_queue.seed(tasks)
        except Exception as e:
            if is_high:
                yield _build_message(
                    f"⚠️ 高风险需求灌队列失败({str(e)[:80]})，已拦截不执行。请人工介入。"
                )
                return
            yield _build_message(
                f"⚠️ 灌队列失败({str(e)[:80]})，降级交互轨执行。"
            )
            async for event in self._resident.send(self._last_text):
                yield event
            return
        yield _build_message(
            f"🌐 交付轨 req_id={req_id}：已灌入 {n} 个任务到队列"
        )
        async for event in self._run_executors(tasks, req_id):
            yield event
    async def _run_executors(
        self, tasks: list[dict], req_id: str
    ) -> AsyncIterator[ChatEvent]:
        """交付轨执行器编排:槽分组 -> worktree -> 串行 spawn -> 验证(merge 前) -> merge。
        都单策略:单 worktree + 串行执行器(按 (module_id,domain) 聚合,每槽一个)。
        续跑:执行器退出但 is_req_done=False 时重 spawn(限 3 次)。
          - 坑1(空转):续前判 has_pending,真空槽不续(is_req_done=False 是别槽的事)。
          - 坑2(claimed 卡):续前 reset_slot_claimed 回收崩溃遗留 claimed。
        判定靠退出码 + is_req_done,不解析 stream-json(stdout 重定向日志)。
        """
        if shutil.which("claude") is None:
            yield _build_message(
                "⚠️ claude CLI 未在 PATH 找到,无法 spawn 执行器,交付中止。"
            )
            return
        slots = sorted({(t["module_id"], t["domain"]) for t in tasks})
        try:
            worktree_path = await asyncio.to_thread(
                create_delivery_worktree, self.project_dir, req_id
            )
        except Exception as e:
            yield _build_message(f"⚠️ worktree 创建失败({str(e)[:80]})，交付中止。")
            return
        svc_dir = os.environ.get("TASK_SERVICE_DIR", "")
        logs_dir = Path(self.project_dir) / ".claude" / "delivery-logs" / req_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        mcp_config_path = str(logs_dir / "mcp-config.json")
        write_mcp_config(
            self._task_queue.task_db_path, svc_dir, mcp_config_path
        )
        all_done = False
        for mod, dom in slots:
            slot_count = sum(
                1 for t in tasks if t["module_id"] == mod and t["domain"] == dom
            )
            max_turns = max(20, slot_count * 5)
            retry = 0
            while retry < 3:
                # 坑2:回收该槽崩溃执行器遗留的 claimed(领了没 report)
                self._task_queue.reset_slot_claimed(mod, dom)
                # 坑1:真空槽不续(is_req_done=False 是别槽的事,等别槽)
                if not self._task_queue.has_pending(mod, dom):
                    break
                log_path = str(
                    logs_dir / f"executor-{mod}-{sanitize_module_id(dom)}-{retry}.log"
                )
                args = build_executor_args(
                    build_loop_prompt(mod, dom), mcp_config_path, max_turns
                )
                # 调度清单:spawn 前写完整 argv + prompt + 禁用能力,供排查"传了啥/砍了啥"
                manifest = build_dispatch_manifest(
                    args,
                    req_id=req_id,
                    module_id=mod,
                    domain=dom,
                    attempt=retry,
                    cwd=worktree_path,
                    log_path=log_path,
                    max_turns=max_turns,
                )
                manifest_path = (
                    logs_dir / f"dispatch-{mod}-{sanitize_module_id(dom)}-{retry}.json"
                )
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                # DRY-RUN:自测看 dispatch 清单用。写完即停,不 spawn claude,不走验证/merge。
                if os.environ.get("HARNESS_DELIVERY_DRY_RUN"):
                    yield _build_message(
                        f"🧪 DRY-RUN: dispatch 清单已写 {manifest_path}\n"
                        f"   跳过 spawn claude(自测用)。worktree 残留可手动删: {worktree_path}"
                    )
                    return
                yield _build_message(
                    f"▶ 槽 {mod}/{dom} 执行器启动 "
                    f"(attempt {retry + 1}/3, max_turns={max_turns})"
                )
                try:
                    proc = await spawn_executor(args, worktree_path, log_path)
                    exit_code = await proc.wait()
                except Exception as e:
                    yield _build_message(
                        f"⚠️ 槽 {mod}/{dom} spawn 失败({str(e)[:80]})"
                    )
                    retry += 1
                    continue
                req_done = self._task_queue.is_req_done(req_id)
                yield _build_message(
                    f"◀ 槽 {mod}/{dom} 退出码 {exit_code}, req_done={req_done}"
                )
                if req_done:
                    all_done = True
                    break
                # 退出后:回收当前轮遗留 claimed(领了没 report),再判该槽真空。
                # has_pending 不含 claimed;不 reset 则 max_turns/崩溃留的 claimed 误判真空 -> break 丢 task。
                self._task_queue.reset_slot_claimed(mod, dom)
                if not self._task_queue.has_pending(mod, dom):
                    break  # 真空才停;claimed 卡住则 has_pending True 续 spawn 重领
                retry += 1
            if all_done:
                break
            if retry >= 3:
                yield _build_message(f"⚠️ 槽 {mod}/{dom} 续 3 次未完成，放弃")
        # ── 收尾:验证(merge 前) -> merge ──
        if not self._task_queue.is_req_done(req_id):
            yield _build_message(f"⚠️ req_id={req_id} 未全完成，不 merge。")
            await asyncio.to_thread(remove_worktree, self.project_dir, req_id)
            return
        await asyncio.to_thread(
            commit_worktree, worktree_path, f"deliver {req_id}"
        )
        # 验证在 worktree 跑(merge 前):失败代码不进主分支,worktree 保留供排查
        vok, vout = await self._run_validation(worktree_path)
        if not vok:
            yield _build_message(
                f"⚠️ 验证失败,req blocked(worktree 保留: {worktree_path}):\n"
                f"{vout[:400]}"
            )
            return
        ok, conflict = await asyncio.to_thread(
            merge_worktree_branch, self.project_dir, req_id
        )
        if not ok:
            yield _build_message(
                f"⚠️ merge 冲突,人工解(worktree 保留: {worktree_path}):\n"
                f"{conflict[:400]}"
            )
            return
        yield _build_message(f"✅ req_id={req_id} 交付完成(merge + 验证通过)")
        await asyncio.to_thread(remove_worktree, self.project_dir, req_id)
    async def _run_validation(self, repo_root: str) -> tuple[bool, str]:
        """L0 merge 前验证(在 worktree 跑):pytest + ruff。命令不存在跳过;非 0 退出失败。
        执行器无 Bash(--disallowed-tools Bash)跑不了测试,验证集中 L0 单点。
        """
        results: list[str] = []
        for cmd in (["pytest", "-q"], ["ruff", "check", "."]):
            try:
                r = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",  # zh-CN 默认 gbk,子进程吐 UTF-8 中文会炸 readerthread
                    errors="replace",
                    timeout=300,
                )
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                results.append(f"$ {' '.join(cmd)} TIMEOUT")
                return False, "\n".join(results)
            if r.returncode != 0:
                results.append(
                    f"$ {' '.join(cmd)}\n{r.stdout[-200:]}\n{r.stderr[-200:]}"
                )
                return False, "\n".join(results)
        return True, ""