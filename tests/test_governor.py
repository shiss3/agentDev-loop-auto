"""Governor 单元测试 — Phase 2 Step 3
覆盖：
- _parse_requirement: stateless query() 需求解析（结构化输出 / RuntimeError）
- _decide_track: 双轨调度纯规则判定（risk_level / change_type / file_count_bucket / cross_domain / 缺省）
- handle_user_input: 自动判定 / forced_track / 异常降级
- _build_resident_session: 执行层配置接线（可写 / 不挂 can_use_tool）
- session property / rebuild
所有测试不拉真实 SDK/CLI/文件系统：
- session_store 用 MagicMock(spec=SessionStore) 避免 ~/.autoloop/sessions 真实 mkdir
- query / _resident.send / _parse_requirement 用 fake 替换
- BaseAgentSession.start/close 在 rebuild 测试中 patch 为 AsyncMock
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SessionStore, ToolUseBlock
from autoloop_agent.chat.events import EventType, text_event
from autoloop_agent.core import architect
from autoloop_agent.core.architect import Governor, validate_modules
# ── 辅助 ──────────────────────────────────────────────
def make_fake_assistant_msg(spec=None, usage=None):
    """构造能通过 isinstance(msg, AssistantMessage) 检查的 fake 消息。
    MagicMock(spec=AssistantMessage) —— spec 让 isinstance 返回 True。
    spec: 填 tool_use block 的 input(任务单 dict;None 表示无 tool_use)。
    usage: 填 msg.usage(dict|None);_parse_requirement 按轮记录进 turns 日志。
    _parse_requirement 从流 AssistantMessage.tool_use 截获 submit_analysis_plan input。
    """
    msg = MagicMock(spec=AssistantMessage)
    msg.usage = usage
    if spec is not None:
        msg.content = [ToolUseBlock(id="1", name="mcp__plan_capture__submit_analysis_plan", input=spec)]
    else:
        msg.content = []
    return msg
def make_fake_result_msg(usage=None):
    """构造 fake ResultMessage(MagicMock spec 让 isinstance 过),带 usage dict。
    usage: 填 msg.usage(dict|None);_parse_requirement 从 ResultMessage.usage 取 tokens。
    """
    msg = MagicMock(spec=ResultMessage)
    msg.usage = usage
    return msg
def make_fake_send(events):
    """构造 fake async generator send（替换 _resident.send）。
    send 是 async generator（`async for event in send(...)`），fake 也必须是。
    返回的 fake_send 可多次调用，每次产出全新生成器。
    """
    async def fake_send(prompt):
        for ev in events:
            yield ev
    return fake_send
def make_governor():
    """构造 Governor，session_store 用 mock 避免真实文件 mkdir。"""
    return Governor(project_dir=".", session_store=MagicMock(spec=SessionStore))
async def collect(agen):
    """收集 async generator 的所有事件。"""
    return [ev async for ev in agen]
def _text_events_containing(events, keyword):
    """筛选 TEXT 事件中 data['text'] 含 keyword 的事件。"""
    return [
        e
        for e in events
        if e.type == EventType.TEXT and keyword in e.data.get("text", "")
    ]
@pytest.fixture
def patch_query(monkeypatch):
    """返回 setter:传 spec dict,patch architect.query 返回它。
    用法:
        patch_query({"task_summary":"...","acceptance_criteria":[],...})
        spec = await governor._parse_requirement("x", context_continuation=False)
    """
    def _set(spec, usage=None):
        async def fake_query(*, prompt, options=None):
            yield make_fake_assistant_msg(spec)
            if usage is not None:
                yield make_fake_result_msg(usage)
        monkeypatch.setattr(architect, "query", fake_query)
    return _set
@pytest.fixture
def stub_executors(monkeypatch):
    """stub _run_executors 为空 async gen(测调度/降级/拦截时不真 spawn claude/git)。"""
    async def _stub(self, modules, req_id, spec):
        if False:
            yield  # 标记 async generator,实际空跑
    monkeypatch.setattr(Governor, "_run_executors", _stub)
# ── 1. _parse_requirement 返回解析结果 ──
async def test_parse_requirement_returns_spec(patch_query):
    """patch_query 返回任务单 → _parse_requirement 返回该 dict"""
    governor = make_governor()
    expected = {
        "task_summary": "改颜色",
        "acceptance_criteria": ["导航栏变蓝"],
        "risk_level": "low",
        "suggest_track": "interactive",
    }
    patch_query(expected)
    spec = await governor._parse_requirement("x", context_continuation=False)
    assert spec == expected
# ── 2. _parse_requirement 无 tool_use -> RuntimeError ──
async def test_parse_requirement_no_output_raises(patch_query):
    """query 流无 submit_analysis_plan tool_use -> _parse_requirement raise RuntimeError"""
    governor = make_governor()
    patch_query(None)  # 无 tool_use
    with pytest.raises(RuntimeError, match="未返回结构化结果"):
        await governor._parse_requirement("x", context_continuation=False)
# ── 2c. _parse_requirement 日志:记录 ResultMessage.usage tokens + 工具调用数 ──
async def test_parse_requirement_logs_usage(tmp_path, patch_query, monkeypatch):
    """设 AUTOLOOP_PARSE_LOG=1 -> 解析后写 usage.jsonl,含 in/out tokens + tool_calls"""
    monkeypatch.setenv("AUTOLOOP_PARSE_LOG", "1")
    governor = Governor(
        project_dir=str(tmp_path), session_store=MagicMock(spec=SessionStore)
    )
    patch_query(
        {"task_summary": "x"},
        usage={"input_tokens": 123, "output_tokens": 45},
    )
    await governor._parse_requirement("x", context_continuation=False)
    log_file = tmp_path / ".claude" / "parse-logs" / "usage.jsonl"
    assert log_file.exists()
    rec = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert rec["input_tokens"] == 123
    assert rec["output_tokens"] == 45
    assert rec["tool_calls"] == 1  # submit_analysis_plan
    assert rec["captured"] is True
    assert rec["duration_s"] >= 0
# ── 2e. 逐轮 token 分布日志(AssistantMessage.usage -> turns) ──
async def test_parse_requirement_logs_turns(tmp_path, monkeypatch):
    """AUTOLOOP_PARSE_LOG=1 -> turns 记录每轮 in/out/cache + 本轮工具名"""
    monkeypatch.setenv("AUTOLOOP_PARSE_LOG", "1")
    governor = Governor(
        project_dir=str(tmp_path), session_store=MagicMock(spec=SessionStore)
    )
    async def fake_query(*, prompt, options=None):
        yield make_fake_assistant_msg(
            usage={"input_tokens": 100, "output_tokens": 10,
                   "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}
        )
        yield make_fake_assistant_msg(
            {"task_summary": "x"},
            usage={"input_tokens": 260, "output_tokens": 20},
        )
    monkeypatch.setattr(architect, "query", fake_query)
    await governor._parse_requirement("x", context_continuation=False)
    rec = json.loads(
        (tmp_path / ".claude" / "parse-logs" / "usage.jsonl")
        .read_text(encoding="utf-8").strip()
    )
    assert rec["turns"] == [
        {"i": 0, "in": 100, "out": 10, "cr": 5, "cc": 3, "tools": []},
        {"i": 1, "in": 260, "out": 20, "cr": 0, "cc": 0,
         "tools": ["mcp__plan_capture__submit_analysis_plan"]},
    ]
# ── 2d. ResultMessage.usage=None 不崩(回归:旧 msg.input_tokens 属性错已修) ──
async def test_parse_requirement_result_msg_none_usage(tmp_path, monkeypatch):
    """ResultMessage.usage=None -> _usage={} 兜底,不 AttributeError,spec 仍截获"""
    monkeypatch.delenv("AUTOLOOP_PARSE_LOG", raising=False)
    governor = Governor(
        project_dir=str(tmp_path), session_store=MagicMock(spec=SessionStore)
    )
    async def fake_query(*, prompt, options=None):
        yield make_fake_assistant_msg({"task_summary": "x"})
        yield make_fake_result_msg(usage=None)
    monkeypatch.setattr(architect, "query", fake_query)
    spec = await governor._parse_requirement("x", context_continuation=False)
    assert spec == {"task_summary": "x"}
# ── 2b. _parse_requirement 不盲拆:options 含 codegraph MCP + submit_analysis_plan tool ──
async def test_parse_requirement_explores_project(monkeypatch):
    """_parse_requirement 配置含 codegraph MCP server + submit_analysis_plan tool(不盲拆)"""
    governor = make_governor()
    captured = {}
    async def fake_query(*, prompt, options=None):
        captured["options"] = options
        yield make_fake_assistant_msg({"task_summary": "x"})
    monkeypatch.setattr(architect, "query", fake_query)
    await governor._parse_requirement("x", context_continuation=False)
    opts = captured["options"]
    # codegraph MCP server 配置(探索工具)
    assert "codegraph" in opts.mcp_servers
    codegraph_cfg = opts.mcp_servers["codegraph"]
    assert codegraph_cfg["command"] == "codegraph"
    assert codegraph_cfg["args"] == ["serve", "--mcp"]
    # plan_capture SDK MCP server(submit_analysis_plan 装载于此)
    assert "plan_capture" in opts.mcp_servers
    # 读工具 + submit_analysis_plan 在 allowed_tools
    assert "mcp__codegraph__codegraph_explore" in opts.allowed_tools
    assert "Read" in opts.allowed_tools
    assert "Glob" in opts.allowed_tools
    assert "Grep" in opts.allowed_tools
    assert "mcp__plan_capture__submit_analysis_plan" in opts.allowed_tools
    # max_turns=40(探索+拆任务+调用工具需多轮)
    assert opts.max_turns == 40
    # 不用 output_format(改用 tool 截获)
    assert opts.output_format is None
    # 写工具/任务工具被禁(解析只读不副作用)
    assert "Bash" in opts.disallowed_tools
    assert "Edit" in opts.disallowed_tools
    assert "Write" in opts.disallowed_tools
    assert "WebSearch" in opts.disallowed_tools
    assert "TaskCreate" in opts.disallowed_tools
    assert "AskUserQuestion" in opts.disallowed_tools

# ── 3. _decide_track 全满足(patch+1-5+cross_domain=False) -> interactive ──
def test_decide_track_all_patch_interactive():
    """spec 全满足交互轨条件 -> interactive"""
    governor = make_governor()
    spec = {
        "change_type": "patch",
        "file_count_bucket": "1-5",
        "cross_domain": False,
    }
    assert governor._decide_track(spec) == "interactive"
# ── 4. _decide_track 任一不满足 -> delivery ──
def test_decide_track_feature_delivery():
    """change_type=feature -> delivery"""
    governor = make_governor()
    spec = {
        "change_type": "feature",
        "file_count_bucket": "1-5",
        "cross_domain": False,
    }
    assert governor._decide_track(spec) == "delivery"
def test_decide_track_many_files_delivery():
    """file_count_bucket=6+ -> delivery"""
    governor = make_governor()
    spec = {
        "change_type": "patch",
        "file_count_bucket": "6+",
        "cross_domain": False,
    }
    assert governor._decide_track(spec) == "delivery"
def test_decide_track_cross_domain_delivery():
    """cross_domain=True -> delivery"""
    governor = make_governor()
    spec = {
        "change_type": "patch",
        "file_count_bucket": "1-5",
        "cross_domain": True,
    }
    assert governor._decide_track(spec) == "delivery"
# ── 5b. _decide_track risk_level=high 即使三字段全中 -> delivery ──
def test_decide_track_risk_high_delivery():
    """risk_level=high 即使三字段全中(patch+1-5+单领域) -> delivery（高风险本层拦死）"""
    governor = make_governor()
    spec = {
        "change_type": "patch",
        "file_count_bucket": "1-5",
        "cross_domain": False,
        "risk_level": "high",
    }
    assert governor._decide_track(spec) == "delivery"
# ── 5. _decide_track 字段缺省 -> delivery ──
def test_decide_track_missing_fields_delivery():
    """字段缺省 -> delivery（保守）"""
    governor = make_governor()
    assert governor._decide_track({}) == "delivery"
# ── 5c. _decide_track request_kind 门(meta/question -> interactive, other 不门) ──
def test_decide_track_meta_request_gated_interactive():
    """request_kind=meta_request 即使 feature/6+/cross_domain(原规则 delivery) -> interactive(门拦截)"""
    governor = make_governor()
    spec = {
        "request_kind": "meta_request",
        "change_type": "feature",
        "file_count_bucket": "6+",
        "cross_domain": True,
    }
    assert governor._decide_track(spec) == "interactive"
def test_decide_track_other_not_gated_delivery():
    """request_kind=other 不门 -> 走原规则(feature/6+/cross_domain -> delivery),other 非逃逸口"""
    governor = make_governor()
    spec = {
        "request_kind": "other",
        "change_type": "feature",
        "file_count_bucket": "6+",
        "cross_domain": True,
    }
    assert governor._decide_track(spec) == "delivery"
def test_decide_track_meta_high_delivery():
    """request_kind=meta_request + risk=high -> delivery(high 安全网先于门,防 high dev_task 误标 meta 漏网)"""
    governor = make_governor()
    spec = {
        "request_kind": "meta_request",
        "change_type": "patch",
        "file_count_bucket": "1-5",
        "cross_domain": False,
        "risk_level": "high",
    }
    assert governor._decide_track(spec) == "delivery"
# ── 6. handle_user_input 自动交互轨 ──
async def test_handle_interactive_auto():
    """mock 解析→interactive + mock _resident.send → events 含调度提示 + send 事件"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(
        return_value={
            "task_summary": "改颜色",
            "acceptance_criteria": [],
            "risk_level": "low",
            "suggest_track": "interactive",
            "change_type": "patch",
            "file_count_bucket": "1-5",
            "cross_domain": False,
        }
    )
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])
    events = await collect(governor.handle_user_input("x"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert len(dispatch_evs) == 1
    assert "interactive" in dispatch_evs[0].data["text"]
    assert fake_ev in events
# ── 7. handle_user_input 交付轨:modules 规范化送 _run_executors ──
async def test_handle_delivery_placeholder(monkeypatch):
    """mock 解析→delivery+modules → events 含 req_id;_run_executors 收规范化模块"""
    captured: dict = {}

    async def fake_executors(self, modules, req_id, spec):
        captured["modules"] = modules
        captured["req_id"] = req_id
        if False:
            yield

    monkeypatch.setattr(Governor, "_run_executors", fake_executors)
    governor = make_governor()
    governor._parse_requirement = AsyncMock(
        return_value={
            "task_summary": "重构支付",
            "acceptance_criteria": ["支付链路全通"],
            "risk_level": "high",
            "suggest_track": "delivery",
            "change_type": "feature",
            "file_count_bucket": "6+",
            "cross_domain": True,
            "modules": [
                {
                    "module_id": "Payment",
                    "summary": "支付模块",
                    "acceptance": ["链路全通"],
                    "subtasks": [
                        {"id": "t1", "summary": "建表", "acceptance": ["表存在"],
                         "intended_files": ["db.sql"]},
                        {"id": "t2", "summary": "支付页", "acceptance": ["渲染"],
                         "intended_files": ["pay.vue"]},
                    ],
                },
            ],
        }
    )
    events = await collect(governor.handle_user_input("重构支付链路"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert len(dispatch_evs) == 1
    assert "delivery" in dispatch_evs[0].data["text"]
    req_id_evs = _text_events_containing(events, "req_id=")
    assert len(req_id_evs) == 1
    # _run_executors 收到规范化模块(module_id normalize 小写)
    assert captured["modules"][0]["module_id"] == "payment"
    assert len(captured["modules"][0]["subtasks"]) == 2
    assert captured["req_id"]
# ── 7b. high+patch+1-5+单领域+modules 非空 -> delivery 不降级 ──
async def test_handle_high_patch_no_degrade(stub_executors):
    """high 风险小补丁(patch+1-5+单领域)但 risk_level=high -> delivery；
    modules 非空 -> 走交付不降级常驻（risk_level 拦截不被降级路径架空）"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(
        return_value={
            "task_summary": "改支付回调签名校验",
            "acceptance_criteria": ["签名校验改用 HMAC-SHA256"],
            "risk_level": "high",
            "suggest_track": "delivery",
            "change_type": "patch",
            "file_count_bucket": "1-5",
            "cross_domain": False,
            "modules": [
                {
                    "module_id": "payment",
                    "summary": "签名校验",
                    "acceptance": ["用 HMAC-SHA256"],
                    "subtasks": [
                        {"id": "t1", "summary": "改签名校验", "acceptance": ["HMAC"],
                         "intended_files": ["callback.py"]},
                    ],
                },
            ],
        }
    )
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])
    events = await collect(governor.handle_user_input("改支付回调签名校验"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert "delivery" in dispatch_evs[0].data["text"]
    # 走交付轨,不降级常驻（send 不被调，无降级事件）
    degrade_evs = _text_events_containing(events, "降级")
    assert len(degrade_evs) == 0
    assert fake_ev not in events
# ── 8. forced_track="interactive" 跳过解析 ──
async def test_handle_forced_interactive_skips_parse():
    """forced_track='interactive' → _parse_requirement 不被调用，events 含调度+send 事件"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(side_effect=AssertionError("不应调用"))
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])
    events = await collect(governor.handle_user_input("x", forced_track="interactive"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert len(dispatch_evs) == 1
    assert "interactive" in dispatch_evs[0].data["text"]
    assert fake_ev in events
    governor._parse_requirement.assert_not_called()
# ── 9. forced_track="delivery" 解析+强制 delivery(modules 非空) ──
async def test_handle_forced_delivery_runs_delivery(monkeypatch):
    """forced_track='delivery' -> 调 _parse_requirement;modules 非空 -> 强制走 _run_delivery"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(return_value={
        "task_summary": "x",
        "modules": [{"module_id": "m", "summary": "s", "acceptance": [],
                     "subtasks": [{"id": "t1", "summary": "s", "acceptance": []}]}],
    })
    delivery_specs = []
    async def fake_delivery(self, spec):
        delivery_specs.append(spec)
        yield text_event("delivery-ev")
    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("x", forced_track="delivery"))
    governor._parse_requirement.assert_called_once()
    assert delivery_specs  # 走了 delivery
    dispatch_evs = _text_events_containing(events, "调度")
    assert any("delivery" in e.data["text"] for e in dispatch_evs)
# ── 9b. forced_track="delivery" modules=[] -> 拦截不执行 ──
async def test_handle_forced_delivery_empty_subtasks_blocked(monkeypatch):
    """forced_track='delivery' + modules=[] -> 拦截提示,不走 _run_delivery"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(return_value={"task_summary": "x", "modules": []})
    async def fake_delivery(self, spec):
        yield text_event("should-not-happen")
    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("x", forced_track="delivery"))
    assert not _text_events_containing(events, "should-not-happen")
    assert _text_events_containing(events, "未拆出模块")
# ── 10. _run_validation 显式 UTF-8 编码(gbk locale 不炸) ──
async def test_run_validation_forces_utf8_encoding(monkeypatch):
    """_run_validation subprocess 必须 encoding='utf-8'+errors='replace'。

    Windows zh-CN text=True 默认 gbk,pytest/ruff 吐 UTF-8 中文 ->
    _readerthread UnicodeDecodeError。
    """
    governor = make_governor()
    captured: list[dict] = []

    def fake_run(*a, **kw):
        captured.append(kw)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    monkeypatch.setattr(architect.subprocess, "run", fake_run)
    ok, _ = await governor._run_validation(".")
    assert ok is True
    assert captured  # 至少 pytest 一次
    for kw in captured:
        assert kw.get("encoding") == "utf-8"
        assert kw.get("errors") == "replace"


# ── resident_plan：常驻会话方案生成工具 ──
def test_resident_session_mounts_plan_tool():
    """_build_resident_session 挂载 plan_gen SDK server + tool_intercept"""
    governor = make_governor()
    session = governor._resident
    opts = session._build_options()
    assert "plan_gen" in opts.mcp_servers
    assert session.tool_intercept == governor._on_tool_use


async def test_capture_plan_handler():
    """propose_plan handler 截获 args 存 _pending_plan，返回确认文本"""
    governor = make_governor()
    spec = {"task_summary": "x", "modules": [{"module_id": "m"}]}
    result = await governor._capture_plan(spec)
    assert governor._pending_plan is spec
    assert result["content"][0]["type"] == "text"


def test_on_tool_use_fallback():
    """流式兜底：plan 工具名且 _pending_plan 为空 -> 补获；已有方案/其他工具 -> 不动"""
    governor = make_governor()
    # 其他工具不截获
    governor._on_tool_use("Read", {"file_path": "x"})
    assert governor._pending_plan is None
    # plan 工具截获
    spec = {"task_summary": "fallback"}
    governor._on_tool_use("mcp__plan_gen__propose_plan", spec)
    assert governor._pending_plan is spec
    # 已有方案不被兜底覆盖（主通道优先）
    governor._on_tool_use("mcp__plan_gen__propose_plan", {"task_summary": "other"})
    assert governor._pending_plan is spec


async def test_rebuild_keeps_plan_tool(monkeypatch):
    """rebuild 后工具仍挂载；_pending_plan 存 Governor 不丢"""
    monkeypatch.setattr(
        "autoloop_agent.core.base_session.BaseAgentSession.start", AsyncMock()
    )
    monkeypatch.setattr(
        "autoloop_agent.core.base_session.BaseAgentSession.close", AsyncMock()
    )
    governor = make_governor()
    governor._pending_plan = {"task_summary": "keep"}
    await governor.rebuild(model="other-model")
    opts = governor._resident._build_options()
    assert "plan_gen" in opts.mcp_servers
    assert governor._resident.tool_intercept == governor._on_tool_use
    assert governor._pending_plan == {"task_summary": "keep"}


# ── resident_plan：采纳闭环（采用方案 -> 直接走完整交付流程）──
def _plan_spec():
    """带 1 模块 2 子任务的待采纳方案（propose_plan 截获格式）。"""
    return {
        "task_summary": "交付用户模块",
        "modules": [
            {
                "module_id": "user",
                "summary": "用户模块",
                "acceptance": ["迁移可跑", "测试通过"],
                "subtasks": [
                    {"id": "t1", "summary": "建用户表", "acceptance": ["迁移可跑"]},
                    {"id": "t2", "summary": "用户 API", "acceptance": ["测试通过"]},
                ],
            },
        ],
    }


async def test_adopt_plan_runs_delivery_and_clears(monkeypatch):
    """采用方案 + 待采纳方案 -> _run_delivery 被调(完整交付流程) -> 走完清空防重复"""
    governor = make_governor()
    governor._pending_plan = _plan_spec()
    delivered = []

    async def fake_delivery(self, spec):
        delivered.append(spec)
        yield text_event("delivery-done")

    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("采用方案"))
    assert len(delivered) == 1
    assert delivered[0] is governor._pending_plan or delivered[0]["task_summary"] == "交付用户模块"
    assert _text_events_containing(events, "delivery-done")
    # 走完清空,防重复交付
    assert governor._pending_plan is None
    # 再次采纳 -> 无待采纳提示,不再交付
    events2 = await collect(governor.handle_user_input("采用方案"))
    assert len(delivered) == 1
    assert "无待采纳方案" in events2[0].data["text"]


async def test_adopt_plan_no_pending(monkeypatch):
    """无待采纳方案 -> 明确提示,不走交付"""
    governor = make_governor()

    async def fake_delivery(self, spec):
        yield text_event("should-not-happen")

    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("采用方案"))
    assert "无待采纳方案" in events[0].data["text"]
    assert not _text_events_containing(events, "should-not-happen")


async def test_adopt_plan_empty_modules(monkeypatch):
    """方案无模块 -> 提示不交付,方案保留"""
    governor = make_governor()
    governor._pending_plan = {"task_summary": "x", "modules": []}

    async def fake_delivery(self, spec):
        yield text_event("should-not-happen")

    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("采用方案"))
    assert "未拆出模块" in events[0].data["text"]
    assert not _text_events_containing(events, "should-not-happen")
    assert governor._pending_plan is not None


# ── _spec_to_modules:规范化(merge 重复 module_id + deps 去未知/自引用)──
def test_spec_to_modules_normalizes_and_merges():
    governor = make_governor()
    spec = {
        "modules": [
            {"module_id": "user-auth", "summary": "s1", "acceptance": ["a1"],
             "subtasks": [{"id": "t1", "summary": "x", "acceptance": []}],
             "deps": ["ghost", "user-auth"]},
            {"module_id": "user_auth", "summary": "s2", "acceptance": ["a2"],
             "subtasks": [{"id": "t2", "summary": "y", "acceptance": []}]},
            {"module_id": "order", "summary": "s3", "acceptance": [],
             "subtasks": [], "deps": ["user_auth"]},
        ]
    }
    req_id, modules = governor._spec_to_modules(spec)
    assert req_id
    assert [m["module_id"] for m in modules] == ["user_auth", "order"]
    ua = modules[0]
    # 重复 module_id 合并:子任务拼接,验收并集
    assert [st["id"] for st in ua["subtasks"]] == ["t1", "t2"]
    assert ua["acceptance"] == ["a1", "a2"]
    # 未知/自引用 dep 被丢
    assert ua["deps"] == []
    assert modules[1]["deps"] == ["user_auth"]


# ── validate_modules:拆分机械校验告警 ──
def _vm_module(mid, files, sub_ids=("t1",)):
    return {
        "module_id": mid,
        "summary": "s",
        "acceptance": [],
        "subtasks": [
            {"id": sid, "summary": "x", "acceptance": [], "intended_files": files}
            for sid in sub_ids
        ],
    }


def test_validate_modules_in_bounds_silent():
    files = [f"f{i}.py" for i in range(8)]
    assert validate_modules([_vm_module("m", files)]) == []


def test_validate_modules_too_small_warns():
    warns = validate_modules([_vm_module("m", ["a.py", "b.py"])])
    assert len(warns) == 1 and "过碎" in warns[0]


def test_validate_modules_too_large_warns():
    files = [f"f{i}.py" for i in range(20)]
    warns = validate_modules([_vm_module("m", files)])
    assert len(warns) == 1 and "过大" in warns[0]


def test_validate_modules_empty_files_warns_serial():
    warns = validate_modules([_vm_module("m", [])])
    assert len(warns) == 1 and "强制串行" in warns[0]


def test_validate_modules_dup_subtask_id_warns():
    files = [f"f{i}.py" for i in range(6)]
    warns = validate_modules([_vm_module("m", files, sub_ids=("t1", "t1"))])
    assert any("重复" in w for w in warns)


# ── _run_module_slot:摘要文件续跑 + commit/验证门 ──


class _FakeProc:
    async def wait(self):
        return 0


def _slot_module(mid="m", sub_ids=("t1", "t2")):
    return {
        "module_id": mid,
        "summary": "s",
        "acceptance": [],
        "subtasks": [
            {"id": sid, "summary": "x", "acceptance": [], "intended_files": [f"{sid}.py"]}
            for sid in sub_ids
        ],
    }


def _write_summary(wt: str, mid: str, done: list[str]):
    d = Path(wt) / ".autoloop"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"- [x] {t}: done" for t in done]
    (d / f"summary-{mid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_module_slot_resume_across_attempts(monkeypatch, tmp_path):
    """第 1 次 spawn 只完成 t1,第 2 次补完 t2 -> True,spawn 2 次,commit+验证各 1 次,摘要被删"""
    governor = make_governor()
    calls = {"spawn": 0, "commit": 0}

    async def fake_spawn(args, cwd, log_path):
        calls["spawn"] += 1
        done = ["t1"] if calls["spawn"] == 1 else ["t1", "t2"]
        _write_summary(cwd, "m", done)
        return _FakeProc()

    monkeypatch.setattr(architect, "spawn_executor", fake_spawn)
    monkeypatch.setattr(
        architect, "commit_worktree",
        lambda wt, msg: calls.__setitem__("commit", calls["commit"] + 1) or True,
    )
    governor._run_validation = AsyncMock(return_value=(True, ""))

    ok = await governor._run_module_slot(
        _slot_module(), "r1", {"task_summary": "x"}, str(tmp_path), tmp_path, asyncio.Queue()
    )
    assert ok is True
    assert calls["spawn"] == 2
    assert calls["commit"] == 1
    governor._run_validation.assert_called_once()
    # 摘要不进主分支:commit 前已删
    assert not (tmp_path / ".autoloop" / "summary-m.md").exists()


async def test_module_slot_three_attempts_incomplete_returns_false(monkeypatch, tmp_path):
    """3 次 spawn 都未完成 -> False,不 commit 不验证"""
    governor = make_governor()
    calls = {"spawn": 0}

    async def fake_spawn(args, cwd, log_path):
        calls["spawn"] += 1
        return _FakeProc()

    monkeypatch.setattr(architect, "spawn_executor", fake_spawn)
    commit_mock = MagicMock()
    monkeypatch.setattr(architect, "commit_worktree", commit_mock)
    governor._run_validation = AsyncMock(return_value=(True, ""))

    ok = await governor._run_module_slot(
        _slot_module(), "r1", {"task_summary": "x"}, str(tmp_path), tmp_path, asyncio.Queue()
    )
    assert ok is False
    assert calls["spawn"] == 3
    commit_mock.assert_not_called()
    governor._run_validation.assert_not_called()


async def test_module_slot_validation_failure_returns_false(monkeypatch, tmp_path):
    """摘要全完成但验证失败 -> False,worktree 保留"""
    governor = make_governor()

    async def fake_spawn(args, cwd, log_path):
        _write_summary(cwd, "m", ["t1", "t2"])
        return _FakeProc()

    monkeypatch.setattr(architect, "spawn_executor", fake_spawn)
    monkeypatch.setattr(architect, "commit_worktree", lambda wt, msg: True)
    governor._run_validation = AsyncMock(return_value=(False, "pytest failed"))

    ok = await governor._run_module_slot(
        _slot_module(), "r1", {"task_summary": "x"}, str(tmp_path), tmp_path, asyncio.Queue()
    )
    assert ok is False


async def test_module_slot_skip_respawn_when_already_done(monkeypatch, tmp_path):
    """摘要已全 [x](崩溃后残留) -> 不再 spawn,直接 commit+验证"""
    governor = make_governor()
    _write_summary(str(tmp_path), "m", ["t1", "t2"])
    spawn_mock = AsyncMock()
    monkeypatch.setattr(architect, "spawn_executor", spawn_mock)
    monkeypatch.setattr(architect, "commit_worktree", lambda wt, msg: True)
    governor._run_validation = AsyncMock(return_value=(True, ""))

    ok = await governor._run_module_slot(
        _slot_module(), "r1", {"task_summary": "x"}, str(tmp_path), tmp_path, asyncio.Queue()
    )
    assert ok is True
    spawn_mock.assert_not_called()


# ── _run_executors 编排:分层/并行门/失败传播/merge 冲突中止 ──


def _orch_module(mid, files, deps=None):
    m = {
        "module_id": mid,
        "summary": "s",
        "acceptance": [],
        "subtasks": [
            {"id": "t1", "summary": "x", "acceptance": [], "intended_files": files}
        ],
    }
    if deps:
        m["deps"] = deps
    return m


@pytest.fixture
def orch_env(monkeypatch, tmp_path):
    """_run_executors 编排测试环境:假 claude on PATH + 假 worktree 函数 + 关 DRY-RUN。"""
    monkeypatch.setattr(architect.shutil, "which", lambda c: "claude")
    monkeypatch.delenv("AUTOLOOP_DELIVERY_DRY_RUN", raising=False)
    calls = {"create": [], "merge": [], "remove": []}

    def fake_create(repo, req_id, mid=None):
        calls["create"].append(mid)
        return str(tmp_path / f"wt-{mid}")

    def fake_merge(repo, req_id, mid=None):
        calls["merge"].append(mid)
        return (True, "")

    def fake_remove(repo, req_id, mid=None):
        calls["remove"].append(mid)

    monkeypatch.setattr(architect, "create_delivery_worktree", fake_create)
    monkeypatch.setattr(architect, "merge_worktree_branch", fake_merge)
    monkeypatch.setattr(architect, "remove_worktree", fake_remove)
    return calls


async def test_executors_parallel_layer_both_merge(orch_env, monkeypatch):
    """文件隔离两模块同层并行:都跑都 merge(声明序),都 remove"""
    governor = make_governor()

    async def fake_slot(self, module, req_id, spec, wt, logs_dir, evq):
        return True

    monkeypatch.setattr(Governor, "_run_module_slot", fake_slot)
    modules = [_orch_module("a", ["a1.py"]), _orch_module("b", ["b1.py"])]
    events = await collect(governor._run_executors(modules, "r1", {"task_summary": "x"}))
    assert sorted(orch_env["create"]) == ["a", "b"]
    assert orch_env["merge"] == ["a", "b"]  # 声明序
    assert sorted(orch_env["remove"]) == ["a", "b"]
    assert _text_events_containing(events, "已 merge 2/2")


async def test_executors_failed_pred_skips_dependent(orch_env, monkeypatch):
    """a 失败 -> 依赖 a 的 b 跳过;独立的 c 照常 merge(部分交付)"""
    governor = make_governor()

    async def fake_slot(self, module, req_id, spec, wt, logs_dir, evq):
        return module["module_id"] != "a"  # a 失败

    monkeypatch.setattr(Governor, "_run_module_slot", fake_slot)
    modules = [
        _orch_module("a", ["a1.py"]),
        _orch_module("b", ["b1.py"], deps=["a"]),
        _orch_module("c", ["c1.py"]),
    ]
    events = await collect(governor._run_executors(modules, "r1", {"task_summary": "x"}))
    assert orch_env["merge"] == ["c"]
    assert _text_events_containing(events, "跳过")
    assert _text_events_containing(events, "已 merge 1/3")


async def test_executors_merge_conflict_aborts(orch_env, monkeypatch):
    """同层 a merge 冲突 -> 整轮中止,b 不 merge,worktree 保留"""
    governor = make_governor()

    async def fake_slot(self, module, req_id, spec, wt, logs_dir, evq):
        return True

    monkeypatch.setattr(Governor, "_run_module_slot", fake_slot)

    def conflict_merge(repo, req_id, mid=None):
        orch_env["merge"].append(mid)
        return (False, "CONFLICT") if mid == "a" else (True, "")

    monkeypatch.setattr(architect, "merge_worktree_branch", conflict_merge)
    modules = [_orch_module("a", ["a1.py"]), _orch_module("b", ["b1.py"])]
    events = await collect(governor._run_executors(modules, "r1", {"task_summary": "x"}))
    assert orch_env["merge"] == ["a"]  # b 未 merge
    assert orch_env["remove"] == []  # 现场保留
    assert _text_events_containing(events, "merge 冲突")
    assert _text_events_containing(events, "已 merge 0/2")


async def test_executors_overlap_forces_serial_layers(orch_env, monkeypatch):
    """文件重叠两模块强制串行:b 的 worktree 在 a merge 后建"""
    governor = make_governor()
    timeline: list[str] = []

    async def fake_slot(self, module, req_id, spec, wt, logs_dir, evq):
        timeline.append(f"slot:{module['module_id']}")
        return True

    monkeypatch.setattr(Governor, "_run_module_slot", fake_slot)

    def tracked_create(repo, req_id, mid=None):
        timeline.append(f"create:{mid}")
        return "wt"

    def tracked_merge(repo, req_id, mid=None):
        timeline.append(f"merge:{mid}")
        return (True, "")

    monkeypatch.setattr(architect, "create_delivery_worktree", tracked_create)
    monkeypatch.setattr(architect, "merge_worktree_branch", tracked_merge)
    modules = [_orch_module("a", ["same.py"]), _orch_module("b", ["same.py"])]
    events = await collect(governor._run_executors(modules, "r1", {"task_summary": "x"}))
    assert timeline == ["create:a", "slot:a", "merge:a", "create:b", "slot:b", "merge:b"]
    assert _text_events_containing(events, "执行分层")


async def test_executors_writes_module_graph_json(orch_env, monkeypatch, tmp_path):
    """依赖图持久化:modules-<req_id>.json 含 layers/edges/模块详情(DRY-RUN 外也写)"""
    import json as _json

    governor = Governor(
        project_dir=str(tmp_path), session_store=MagicMock(spec=SessionStore)
    )

    async def fake_slot(self, module, req_id, spec, wt, logs_dir, evq):
        return True

    monkeypatch.setattr(Governor, "_run_module_slot", fake_slot)
    modules = [
        _orch_module("a", ["a1.py"]),
        _orch_module("b", ["b1.py"], deps=["a"]),
    ]
    await collect(governor._run_executors(modules, "r1", {"task_summary": "做设置页"}))
    graph_path = tmp_path / ".claude" / "delivery-logs" / "r1" / "modules-r1.json"
    graph = _json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph["req_id"] == "r1"
    assert graph["task_summary"] == "做设置页"
    assert graph["layers"] == [["a"], ["b"]]  # dep 提示边 -> 串行两层
    assert graph["edges"] == [["a", "b"]]
    by_id = {m["module_id"]: m for m in graph["modules"]}
    assert by_id["b"]["deps"] == ["a"]
    assert by_id["a"]["file_set"] == ["a1.py"]
    assert by_id["a"]["subtasks"][0]["id"] == "t1"
