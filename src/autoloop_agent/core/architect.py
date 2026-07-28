"""Governor — L0 治理者（模块粒度交付轨）。
职责：
1. 需求解析：独立 stateless query() + submit_analysis_plan tool（带项目探索），输出标准化任务单，不进常驻 session 历史
2. 双轨调度：纯规则判定交互轨 / 交付轨（任务规模 + 上下文连续性），不调 LLM
3. 交互轨直执行：经 self._resident.send() 产出 ChatEvent 流（常驻执行体，上下文连续）
4. 解析异常降级：_parse_requirement 异常 → 降级交互轨直接执行，不阻断用户
核心铁则：L0 绝不下场编码。判定与执行物理分离：
- 判定 session：stateless query()，tools=[]（只读，不挂业务写工具）
- 交互轨执行体：常驻 BaseAgentSession（可写，复用上下文）
- 交付轨执行体：模块粒度 claude -p 子进程 + per-module worktree；
  全栈单执行器 all-at-once 收全量子任务,摘要文件续跑;同层无依赖+文件隔离模块并行。
不再有 lane guard —— 执行体本身可写，判定 session 本身不挂业务写工具。
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
from autoloop_agent.chat.events import ChatEvent
from autoloop_agent.chat.session_store import create_session_store
from autoloop_agent.core.base_session import BaseAgentSession
from autoloop_agent.core.executor import (
    build_dispatch_manifest,
    build_executor_args,
    build_module_prompt,
    read_summary_completed,
    spawn_executor,
    summary_rel_path,
)
from autoloop_agent.core.module_affinity import normalize_module_id
from autoloop_agent.core.module_scheduler import (
    build_execution_layers,
    build_module_edges,
    module_file_set,
)
from autoloop_agent.core.utils import _build_message
from autoloop_agent.core.worktree import (
    commit_worktree,
    create_delivery_worktree,
    merge_worktree_branch,
    remove_worktree,
)
# ── 常量 ──────────────────────────────────────────────────────────────
# 标准化任务单 schema —— 模块粒度(modules):业务功能域垂直聚合,全栈执行器 all-at-once
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
        "modules": {  # 复杂需求按业务功能域拆模块(feature/6+/cross_domain/high 触发)
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "module_id": {"type": "string"},  # 业务能力英文 slug(小写+下划线),需求内唯一
                    "summary": {"type": "string"},  # 模块目标(自包含)
                    "acceptance": {  # 模块级可验证验收要点
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "subtasks": {  # 模块内开发步骤,执行器一次性全收自主排序
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},  # 模块内唯一短 id("t1","t2",...)
                                "summary": {"type": "string"},
                                "acceptance": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "intended_files": {  # 预期改动文件(探索后填,禁空)
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["id", "summary", "acceptance"],
                        },
                    },
                    "deps": {  # 依赖的同批 module_id 列表(硬依赖提示,无依赖留空)
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["module_id", "summary", "acceptance", "subtasks"],
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
0. 先判 request_kind（分类约定见下"request_kind 分类"）：meta_request/question 不探索代码、不拆 modules，直接跳第 2 步。
1. 探索项目（仅 dev_task）：用 codegraph_explore 查符号/文件/调用路径，用 Read/Grep/Glob 读相关模块。
   先看代码再拆任务，intended_files 才有依据。不探索直接拆=盲拆，禁止。
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
- modules：按【业务功能域】垂直聚合拆分。拆分规则：
  当 change_type=feature 或 file_count_bucket=6+ 或 cross_domain=true 或 risk_level=high 时**必须拆 modules**；
  否则不拆（modules 为空数组）。high 小补丁也拆（整个补丁当 1 个模块 1 个子任务，保 delivery 隔离）。
  模块 = 一项完整用户业务能力的全栈切片：数据表 + 接口 + 前端页面/组件放同一模块
  （例：user_auth = 登录页 + 鉴权 API + users 表）。一个模块由一个执行器端到端完成，
  模块内依赖（先建表再 API 再前端）由执行器内部消化，不上调度层。
  粒度硬约束：每模块全部子任务 intended_files 并集 5~15 个源码文件。
  - <5 文件：过碎，向上归并到相关业务能力的父模块（附属小功能不独立成模块）。
  - >15 文件：过大，按子能力再拆——两半文件无交集即可拆；子域间有逻辑硬依赖的拆开后
    填 deps 串行，不要为躲依赖硬捏成大模块。
  多模块拆分按依赖类型分三类处理：
  - 文件重叠：必须收进同一模块（调度层对文件重叠强制串行，拆开无并行收益只有 merge 开销）。
  - 逻辑硬依赖但文件不重叠：拆成独立模块并显式填 deps。调度层按 deps 分层串行，
    被依赖模块先 merge，依赖方 worktree 从含上游产物的 main 拉分支，能正常编译/测试。
  - 软偏好（顺序建议非硬依赖）：拆，不填 deps，保并行。
  每项：
  - module_id：业务能力的英文 slug（小写+下划线，如 payment/user_auth/session），需求内唯一。
  - summary：模块核心目标（自包含，执行器看不到你的上下文）。
  - acceptance：模块级可验证验收要点。
  - subtasks：模块内开发步骤清单（建表/接口/前端各一步）。每项：
    id（模块内唯一 "t1","t2",...）、summary、acceptance、intended_files（探索后填，禁止留空——
    留空=无法判定文件隔离=被强制与其他模块串行，并行机会丧失）。
  - deps：依赖的同批 module_id 列表。逻辑硬依赖必填（如"订单模块依赖用户模块的 users 表"
    ——漏填=两模块判为无依赖并行跑，依赖方看不到上游产物直接失败）；软偏好不填。
    文件重叠会被调度层强制串行，与此无关。
## 调用指引
- 分析完成后必须调用 submit_analysis_plan 提交任务单，不要只输出文本。
- submit_analysis_plan 参数 = 任务单各字段（task_summary/acceptance_criteria/.../modules），
  schema 与上述字段约定一致。
- 调用后工具返回确认字符串，解析结束，不要再输出其他内容。
## 纪律
- 严格锚定原始需求，所有验收项可追溯到需求原文。
- 模块 summary/subtasks 描述必须自包含（执行器看不到你的上下文）。
- deps 按真实硬跨模块依赖必填（漏填=依赖方并行起跑、看不到上游产物而失败），软偏好不填；
  模块内步骤顺序由执行器自主决定，无需标注。
- 仅做语义层面的歧义补全，遵循项目通用规范与行业默认最佳实践；补全决策在 task_summary
  或 acceptance_criteria 中体现可追溯性。
- 模糊时取保守（change_type 倾向 feature、file_count_bucket 倾向 6+、cross_domain 倾向 true、
  risk_level 倾向 high，即倾向 delivery）。
- track 由代码规则消费 risk_level/change_type/file_count_bucket/cross_domain 判定，suggest_track 仅供参考。
- 不指导实现方案、不指定技术栈、不替模型做执行层决策。
## request_kind 分类（先判类型再拆）
- dev_task：改代码/加功能/修 bug 的开发任务 -> 走完整流程拆 modules。
- meta_request：要你产出文本（生成需求/文档/方案/示例）而非改代码 -> modules=[]，risk=low，不探索代码。
- question：问答/解释/分析 -> modules=[]，risk=low。
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
- modules：[]（空数组）
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
def validate_modules(modules: list[dict]) -> list[str]:
    """拆分机械校验,返回告警串列表(accept-with-warning,不重解析)。

    - 模块文件数(子任务 intended_files 归一并集)<5: 过碎,建议并入父模块;>15: 过大,建议再拆
      (子域文件无交集即可拆,逻辑硬依赖填 deps 串行,勿为躲依赖合一)
    - intended_files 全空: 无法判定文件隔离,将被强制与一切串行
    - 模块内 subtask id 重复: 完成判定按集合,重复 id 会提前判完成
    """
    warnings: list[str] = []
    for m in modules:
        mid = m["module_id"]
        n = len(module_file_set(m))
        if n == 0:
            warnings.append(f"模块 {mid} intended_files 全空,无法判定文件隔离,将被强制串行")
        elif n < 5:
            warnings.append(f"模块 {mid} 仅 {n} 文件,过碎,建议并入父模块")
        elif n > 15:
            warnings.append(
                f"模块 {mid} 达 {n} 文件,过大,建议按子能力再拆"
                "(子域文件无交集即可拆,逻辑硬依赖填 deps 串行,勿为躲依赖合一)"
            )
        ids = [st["id"] for st in m.get("subtasks") or []]
        if len(ids) != len(set(ids)):
            warnings.append(f"模块 {mid} 子任务 id 重复,完成判定可能提前")
    return warnings


# 明显非需求输入(寒暄/确认/道别)白名单 -- handle_user_input 直接走交互轨跳过 LLM 解析省 token。
# 仅抓最无歧义词;真任务(含动词/文件名/需求描述)不在此列,走 _parse_requirement。
# 采纳确认白名单（resident_plan 闭环）：用户回复这些短语时消费 _pending_plan 走交付流程。
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
    - 交付轨:模块粒度执行器 + per-module worktree,Kahn 分层(同层并行/层间串行)
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
        # 用户回复"采用方案"时消费（直接走完整交付流程）。
        self._pending_plan: dict | None = None
        self._resident = self._build_resident_session()
        self._last_text: str | None = None
        # parse_flow 产物(双轨交付队列消费):最近一次的 spec 与判定轨道
        self.last_spec: dict | None = None
        self.last_track: str = "interactive"
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
          TASK_SPEC_SCHEMA，输出与计划格式一致；恒挂载）
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
                "产出实施计划时调用此工具；方案被记录为待采纳，用户回复\"采用方案\"后进入交付流程。"
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
        _turns: list[dict] = []  # 逐轮 token 分布(判定冷启动大头:盲探 vs 定向探索)
        _start = datetime.datetime.now()
        try:
            async for msg in query(prompt=prompt, options=opts):
                # 流兜底:handler 未触发时从 AssistantMessage 提取 tool_use input
                if isinstance(msg, AssistantMessage):
                    _tools: list[str] = []
                    for block in msg.content:
                        if not isinstance(block, ToolUseBlock):
                            continue
                        tool_calls += 1
                        _tools.append(block.name)
                        if (block.name == "mcp__plan_capture__submit_analysis_plan"
                                and captured_spec is None):
                            captured_spec = block.input
                    # AssistantMessage.usage = 单次 API 调用 token(dict)
                    # in 增量≈上轮工具结果 token -> 归因到 tools 列出的调用
                    if _usage := getattr(msg, "usage", None):
                        _turns.append({
                            "i": len(_turns),
                            "in": _usage.get("input_tokens", 0) or 0,
                            "out": _usage.get("output_tokens", 0) or 0,
                            "cr": _usage.get("cache_read_input_tokens", 0) or 0,
                            "cc": _usage.get("cache_creation_input_tokens", 0) or 0,
                            "tools": _tools,
                        })
                elif isinstance(msg, ResultMessage):
                    # ResultMessage.usage 是 dict(SDK 不平铺 input_tokens)
                    _usage = msg.usage or {}
                    in_tokens += _usage.get("input_tokens", 0) or 0
                    out_tokens += _usage.get("output_tokens", 0) or 0
        finally:
            # 自测日志(默认关,设 AUTOLOOP_PARSE_LOG=1 开):解析阶段 in/out tokens + 工具调用数
            # + turns 逐轮分布(i/in/out/cr=cache_read/cc=cache_create/tools)
            # -> .claude/parse-logs/usage.jsonl。finally 兜底:解析异常也写部分值供排查。
            if os.environ.get("AUTOLOOP_PARSE_LOG"):
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
                    "turns": _turns,
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
    def _spec_to_modules(self, spec: dict) -> tuple[str, list[dict]]:
        """spec.modules -> 规范化模块列表（normalize module_id + 重复合并 + deps 去未知/自引用）。
        交付轨（_run_delivery）与采纳闭环（_adopt_plan）共用。"""
        raw = spec.get("modules") or []
        req_id = self._new_req_id()
        merged: dict[str, dict] = {}
        order: list[str] = []
        for m in raw:
            mid = normalize_module_id(m.get("module_id", "default"))
            if mid not in merged:
                merged[mid] = {
                    "module_id": mid,
                    "summary": m.get("summary", ""),
                    "acceptance": list(m.get("acceptance") or []),
                    "subtasks": list(m.get("subtasks") or []),
                    "deps": list(m.get("deps") or []),
                }
                order.append(mid)
            else:  # 重复 module_id 合并:子任务拼接,验收/依赖并集
                tgt = merged[mid]
                tgt["subtasks"].extend(m.get("subtasks") or [])
                tgt["acceptance"] = sorted(set(tgt["acceptance"]) | set(m.get("acceptance") or []))
                tgt["deps"] = sorted(set(tgt["deps"]) | set(m.get("deps") or []))
        known = set(order)
        for mid in order:
            deps = {normalize_module_id(d) for d in merged[mid]["deps"]}
            merged[mid]["deps"] = sorted(d for d in deps if d in known and d != mid)
        return req_id, [merged[mid] for mid in order]
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
        """采纳闭环：待采纳方案直接走完整交付流程（worktree + spawn 执行器 + merge）。
        交付流程走完才清 _pending_plan；异常中断保留方案可重试。"""
        plan = self._pending_plan
        if plan is None:
            yield _build_message(
                "⚠️ 无待采纳方案。请先在对话中让常驻执行体生成方案（propose_plan）。"
            )
            return
        if not (plan.get("modules") or []):
            yield _build_message(
                "⚠️ 方案未拆出模块，无法交付（方案保留，可要求常驻执行体重新生成）。"
            )
            return
        async for event in self._run_delivery(plan):
            yield event
        self._pending_plan = None
    # ── 双轨交付队列公开入口(chat 层 DeliveryRunner 消费) ──
    def is_adoption_input(self, text: str) -> bool:
        """公开采纳判定(chat 层分流用):命中则该输入应入交付队列而非发交互轨。"""
        return self._is_adoption_input(text)

    async def adopt_flow(self) -> AsyncIterator[ChatEvent]:
        """采纳段公开入口,透传 _adopt_plan(保留 monkeypatch 点)。"""
        async for event in self._adopt_plan():
            yield event

    async def deliver_flow(self, spec: dict) -> AsyncIterator[ChatEvent]:
        """交付段公开入口,透传 _run_delivery(保留 monkeypatch 点)。"""
        async for event in self._run_delivery(spec):
            yield event

    async def parse_flow(self, text: str) -> AsyncIterator[ChatEvent]:
        """auto 解析段:寒暄短路 + 🔍 开始 + _parse_requirement + 🔀 调度。
        结果存 self.last_spec / self.last_track;不碰 resident,
        交互执行由调用方按 last_track 路由(交付队列只跑解析+交付)。
        """
        self._last_text = text
        self.last_spec = None
        self.last_track = "interactive"
        if self._is_casual_input(text):
            # 寒暄/确认等非需求输入直接常驻交互,跳过 LLM 解析省 token
            self.last_spec = {
                "task_summary": text,
                "acceptance_criteria": [],
                "risk_level": "low",
                "suggest_track": "interactive",
            }
            yield _build_message(f"🔀 调度: interactive（{text[:40]}）")
            return
        yield _build_message(f"🔍 解析需求: {text[:40]}")
        try:
            spec = await self._parse_requirement(
                text,
                context_continuation=self._resident.stats.turn_count > 0,
            )
        except Exception as e:
            yield _build_message(
                f"⚠️ 需求解析异常({str(e)[:80]}),已降级为交互轨直接执行。"
            )
            return
        self.last_spec = spec
        self.last_track = self._decide_track(spec)
        yield _build_message(
            f"🔀 调度: {self.last_track}（{spec.get('task_summary', '')[:40]}）"
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
            # 采纳闭环：消费待采纳方案走交付流程，优先于一切解析/调度
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
            if not (spec.get("modules") or []):
                yield _build_message(
                    "⚠️ 强制交付轨但解析未拆出模块(输入可能非开发任务)。"
                    "请重述为开发需求,或用 /semi 切换半自动模式直通交互轨。"
                )
                return
            track = "delivery"
        else:
            # auto:解析段组合(🔍/🔀/降级告警由 parse_flow 产出)
            async for event in self.parse_flow(text):
                yield event
            if self.last_track == "interactive":
                async for event in self._resident.send(text):
                    yield event
                return
            async for event in self._run_delivery(self.last_spec):
                yield event
            return
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
        """交付轨：modules 规范化 -> 校验告警 -> _run_executors 分层执行。"""
        modules_raw = spec.get("modules") or []
        is_high = spec.get("risk_level") == "high"
        if not modules_raw:
            if is_high:
                yield _build_message(
                    "⚠️ 高风险需求但解析未拆出模块（模型违规），已拦截不执行（常驻不直写高风险）。请人工介入或重述需求。"
                )
                return
            yield _build_message("⚠️ 交付轨无模块，降级交互轨执行。")
            async for event in self._resident.send(self._last_text):
                yield event
            return
        req_id, modules = self._spec_to_modules(spec)
        for w in validate_modules(modules):
            yield _build_message(f"⚠️ 拆分校验: {w}")
        yield _build_message(
            f"🌐 交付轨 req_id={req_id}：{len(modules)} 个模块"
        )
        async for event in self._run_executors(modules, req_id, spec):
            yield event
    async def _run_executors(
        self, modules: list[dict], req_id: str, spec: dict
    ) -> AsyncIterator[ChatEvent]:
        """交付轨执行器编排:Kahn 分层 -> 逐层(建 worktree -> 层内并发跑模块 -> 顺序 merge)。
        并行门:同层模块无跨模块依赖且文件无交集(module_scheduler 保证);其余拓扑串行。
        失败策略:失败模块阻塞其传递后继;独立模块照常 merge(部分交付)。
        merge 冲突:整轮中止,保留现场人工解。
        """
        if shutil.which("claude") is None:
            yield _build_message(
                "⚠️ claude CLI 未在 PATH 找到,无法 spawn 执行器,交付中止。"
            )
            return
        layers = build_execution_layers(modules)
        edges = build_module_edges(modules)
        preds: dict[str, set[str]] = {}
        for a, b in edges:
            preds.setdefault(b, set()).add(a)
        by_id = {m["module_id"]: m for m in modules}
        logs_dir = Path(self.project_dir) / ".claude" / "delivery-logs" / req_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        # 模块依赖图持久化:DRY-RUN/真跑都写,供事后查看模块划分/边/分层。
        graph = {
            "req_id": req_id,
            "task_summary": spec.get("task_summary", ""),
            "layers": layers,
            "edges": sorted([a, b] for a, b in edges),
            "modules": [
                {
                    "module_id": m["module_id"],
                    "summary": m.get("summary", ""),
                    "acceptance": m.get("acceptance", []),
                    "deps": m.get("deps", []),
                    "file_set": sorted(module_file_set(m)),
                    "subtasks": m["subtasks"],
                }
                for m in modules
            ],
        }
        (logs_dir / f"modules-{req_id}.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if len(layers) > 1 or any(len(layer) > 1 for layer in layers):
            shape = " -> ".join(f"[{'|'.join(layer)}]" for layer in layers)
            yield _build_message(f"🧭 执行分层: {shape}")
        # DRY-RUN:自测看 dispatch 清单用。逐模块写 manifest(不建 worktree),不 spawn,不 merge。
        if os.environ.get("AUTOLOOP_DELIVERY_DRY_RUN"):
            for layer in layers:
                for mid in layer:
                    m = by_id[mid]
                    max_turns = max(40, 20 * len(m["subtasks"]))
                    prompt = build_module_prompt(
                        m, spec.get("task_summary", ""), summary_rel_path(mid)
                    )
                    args = build_executor_args(prompt, max_turns)
                    manifest = build_dispatch_manifest(
                        args, req_id=req_id, module_id=mid, attempt=0,
                        cwd=f"<worktree:deliver-{req_id}-{mid}>",
                        log_path=f"<log:executor-{mid}-0.log>",
                        max_turns=max_turns,
                    )
                    manifest_path = logs_dir / f"dispatch-{mid}-0.json"
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            yield _build_message(
                f"🧪 DRY-RUN: {len(modules)} 份 dispatch 清单已写 {logs_dir}(自测用,未 spawn)"
            )
            return
        failed: set[str] = set()
        merged: list[str] = []
        aborted = False
        for layer in layers:
            if aborted:
                break
            runnable = []
            for mid in layer:
                if preds.get(mid, set()) & failed:
                    failed.add(mid)
                    yield _build_message(f"⏭ 模块 {mid} 跳过(上游失败/跳过)")
                else:
                    runnable.append(mid)
            if not runnable:
                continue
            # 建 worktree 顺序进行(避 git worktree add 锁竞争);
            # 第 k 层分支自含前 k-1 层 merge 的 main
            worktrees: dict[str, str] = {}
            for mid in runnable:
                try:
                    worktrees[mid] = await asyncio.to_thread(
                        create_delivery_worktree, self.project_dir, req_id, mid
                    )
                except Exception as e:
                    yield _build_message(f"⚠️ 模块 {mid} worktree 创建失败({str(e)[:80]})")
                    failed.add(mid)
            runnable = [mid for mid in runnable if mid in worktrees]
            if not runnable:
                continue
            # 层内并发跑,事件经 Queue 扇入保流式
            evq: asyncio.Queue = asyncio.Queue()

            async def _slot_runner(m: dict, wt: str) -> bool:
                try:
                    ok = await self._run_module_slot(m, req_id, spec, wt, logs_dir, evq)
                except Exception as e:
                    await evq.put(_build_message(
                        f"⚠️ 模块 {m['module_id']} 执行异常({str(e)[:80]})"
                    ))
                    ok = False
                await evq.put(("done", m["module_id"], ok))
                return ok

            tasks = [
                asyncio.create_task(_slot_runner(by_id[mid], worktrees[mid]))
                for mid in runnable
            ]
            pending = len(runnable)
            results: dict[str, bool] = {}
            while pending:
                item = await evq.get()
                if isinstance(item, ChatEvent):
                    yield item
                else:  # ("done", mid, ok)
                    _, mid, ok = item
                    results[mid] = ok
                    pending -= 1
            await asyncio.gather(*tasks, return_exceptions=True)
            # merge 顺序进行(声明序)
            for mid in runnable:
                if not results.get(mid):
                    failed.add(mid)
                    continue
                ok, conflict = await asyncio.to_thread(
                    merge_worktree_branch, self.project_dir, req_id, mid
                )
                if not ok:
                    failed.add(mid)
                    yield _build_message(
                        f"⚠️ 模块 {mid} merge 冲突,整轮中止,人工解"
                        f"(worktree 保留: {worktrees[mid]}):\n{conflict[:400]}"
                    )
                    aborted = True
                    break
                await asyncio.to_thread(remove_worktree, self.project_dir, req_id, mid)
                merged.append(mid)
                yield _build_message(f"✅ 模块 {mid} 交付完成(merge + 验证通过)")
        yield _build_message(
            f"🏁 req_id={req_id} 交付结束: 已 merge {len(merged)}/{len(modules)}"
            + (f",未交付: {','.join(m['module_id'] for m in modules if m['module_id'] not in merged)}" if len(merged) < len(modules) else "")
        )
    async def _run_module_slot(
        self,
        module: dict,
        req_id: str,
        spec: dict,
        worktree_path: str,
        logs_dir: Path,
        evq: asyncio.Queue,
    ) -> bool:
        """单模块执行槽:最多 3 次 spawn(摘要文件续跑) -> 删摘要 -> commit -> 验证(merge 前)。
        完成判定: 子任务 id 集 ⊆ 摘要 [x] 集(替代旧 is_req_done/has_pending/reset_claimed)。
        worktree 由调用方在重试循环外创建一次(create 会 force 删已有路径,不可每 attempt 调)。
        """
        mid = module["module_id"]
        sub_ids = {st["id"] for st in module["subtasks"]}
        max_turns = max(40, 20 * len(module["subtasks"]))
        prompt = build_module_prompt(
            module, spec.get("task_summary", ""), summary_rel_path(mid)
        )
        for attempt in range(3):
            if sub_ids <= read_summary_completed(worktree_path, mid):
                break
            log_path = str(logs_dir / f"executor-{mid}-{attempt}.log")
            args = build_executor_args(prompt, max_turns)
            # 调度清单:spawn 前写完整 argv + prompt + 禁用能力,供排查"传了啥/砍了啥"
            manifest = build_dispatch_manifest(
                args, req_id=req_id, module_id=mid, attempt=attempt,
                cwd=worktree_path, log_path=log_path, max_turns=max_turns,
            )
            (logs_dir / f"dispatch-{mid}-{attempt}.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await evq.put(_build_message(
                f"▶ 模块 {mid} 执行器启动 (attempt {attempt + 1}/3, max_turns={max_turns})"
            ))
            try:
                proc = await spawn_executor(args, worktree_path, log_path)
                try:
                    exit_code = await proc.wait()
                except BaseException:
                    # 取消/异常时杀 executor 子进程,防孤儿继续写 worktree
                    if proc.returncode is None:
                        proc.kill()
                    raise
            except Exception as e:
                await evq.put(_build_message(f"⚠️ 模块 {mid} spawn 失败({str(e)[:80]})"))
                continue
            await evq.put(_build_message(f"◀ 模块 {mid} 退出码 {exit_code}"))
        done = read_summary_completed(worktree_path, mid)
        if not sub_ids <= done:
            await evq.put(_build_message(
                f"⚠️ 模块 {mid} 3 次尝试未完成,缺 {sorted(sub_ids - done)}(worktree 保留: {worktree_path})"
            ))
            return False
        # 摘要不进主分支
        summary_file = Path(worktree_path) / summary_rel_path(mid)
        summary_file.unlink(missing_ok=True)
        await asyncio.to_thread(
            commit_worktree, worktree_path, f"deliver {req_id} {mid}"
        )
        # 验证在 worktree 跑(merge 前):失败代码不进主分支,worktree 保留供排查
        vok, vout = await self._run_validation(worktree_path)
        if not vok:
            await evq.put(_build_message(
                f"⚠️ 模块 {mid} 验证失败,worktree 保留({worktree_path}):\n{vout[:400]}"
            ))
            return False
        return True
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