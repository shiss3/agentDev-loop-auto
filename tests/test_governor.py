"""Governor 单元测试 — Phase 2 Step 3
覆盖：
- _parse_requirement: stateless query() 需求解析（结构化输出 / RuntimeError）
- _decide_track: 双轨调度纯规则判定（risk_level / change_type / file_count_bucket / cross_domain / 缺省）
- handle_user_input: 自动判定 / forced_track / 异常降级
- _build_resident_session: 执行层配置接线（可写 / 不挂 can_use_tool）
- session property / rebuild
所有测试不拉真实 SDK/CLI/文件系统：
- session_store 用 MagicMock(spec=SessionStore) 避免 ~/.harness/sessions 真实 mkdir
- query / _resident.send / _parse_requirement 用 fake 替换
- BaseAgentSession.start/close 在 rebuild 测试中 patch 为 AsyncMock
"""
from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SessionStore, ToolUseBlock
from harness_agent.chat.events import EventType, text_event
from harness_agent.core import architect
from harness_agent.core.architect import Governor
from harness_agent.core.task_queue_adapter import TaskQueueAdapter
# ── 辅助 ──────────────────────────────────────────────
def make_fake_assistant_msg(spec=None):
    """构造能通过 isinstance(msg, AssistantMessage) 检查的 fake 消息。
    MagicMock(spec=AssistantMessage) —— spec 让 isinstance 返回 True。
    spec: 填 tool_use block 的 input(任务单 dict;None 表示无 tool_use)。
    _parse_requirement 从流 AssistantMessage.tool_use 截获 submit_analysis_plan input。
    """
    msg = MagicMock(spec=AssistantMessage)
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
    """stub _run_executors 为空 async gen(测 seed/降级/拦截时不真 spawn claude/git)。"""
    async def _stub(self, tasks, req_id):
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
    """设 HARNESS_PARSE_LOG=1 -> 解析后写 usage.jsonl,含 in/out tokens + tool_calls"""
    monkeypatch.setenv("HARNESS_PARSE_LOG", "1")
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
# ── 2d. ResultMessage.usage=None 不崩(回归:旧 msg.input_tokens 属性错已修) ──
async def test_parse_requirement_result_msg_none_usage(tmp_path, monkeypatch):
    """ResultMessage.usage=None -> _usage={} 兜底,不 AttributeError,spec 仍截获"""
    monkeypatch.delenv("HARNESS_PARSE_LOG", raising=False)
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
# ── 7. handle_user_input 交付轨灌队列 ──
async def test_handle_delivery_placeholder(stub_executors):
    """mock 解析→delivery+subtasks + mock task_queue → events 含 req_id + seed 被调"""
    task_queue = MagicMock(spec=TaskQueueAdapter)
    task_queue.seed.return_value = 2
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        task_queue=task_queue,
    )
    governor._parse_requirement = AsyncMock(
        return_value={
            "task_summary": "重构支付",
            "acceptance_criteria": ["支付链路全通"],
            "risk_level": "high",
            "suggest_track": "delivery",
            "change_type": "feature",
            "file_count_bucket": "6+",
            "cross_domain": True,
            "subtasks": [
                {
                    "id": "t1",
                    "domain": "backend",
                    "module_id": "payment",
                    "summary": "建表",
                    "acceptance": ["表存在"],
                    "intended_files": ["db.sql"],
                    "deps": [],
                },
                {
                    "id": "t2",
                    "domain": "frontend",
                    "module_id": "payment",
                    "summary": "支付页",
                    "acceptance": ["页面渲染"],
                    "intended_files": ["pay.vue"],
                    "deps": ["t1"],
                },
            ],
        }
    )
    events = await collect(governor.handle_user_input("重构支付链路"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert len(dispatch_evs) == 1
    assert "delivery" in dispatch_evs[0].data["text"]
    # 含 req_id 的结果行
    req_id_evs = _text_events_containing(events, "req_id=")
    assert len(req_id_evs) == 1
    # seed 被调用，收到 2 个任务
    assert task_queue.seed.call_count == 1
    seeded = task_queue.seed.call_args.args[0]
    assert len(seeded) == 2
    # 全 id = {req_id}-{short_id}
    t1 = seeded[0]
    assert t1["id"].endswith("-t1")
    assert t1["module_id"] == "payment"
    # deps 短 id -> 全 id 映射：t2.deps == [t1 全 id]
    t2 = seeded[1]
    assert t2["deps"] == [t1["id"]]
    assert t2["module_id"] == "payment"
# ── 7b. high+patch+1-5+单领域+subtasks 非空 -> delivery 灌队列不降级 ──
async def test_handle_high_patch_no_degrade(stub_executors):
    """high 风险小补丁(patch+1-5+单领域)但 risk_level=high -> delivery；
    subtasks 非空 -> 灌队列不降级常驻（risk_level 拦截不被降级路径架空）"""
    task_queue = MagicMock(spec=TaskQueueAdapter)
    task_queue.seed.return_value = 1
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        task_queue=task_queue,
    )
    governor._parse_requirement = AsyncMock(
        return_value={
            "task_summary": "改支付回调签名校验",
            "acceptance_criteria": ["签名校验改用 HMAC-SHA256"],
            "risk_level": "high",
            "suggest_track": "delivery",
            "change_type": "patch",
            "file_count_bucket": "1-5",
            "cross_domain": False,
            "subtasks": [
                {
                    "id": "t1",
                    "domain": "backend",
                    "module_id": "payment",
                    "summary": "改签名校验",
                    "acceptance": ["用 HMAC-SHA256"],
                    "intended_files": ["callback.py"],
                    "deps": [],
                },
            ],
        }
    )
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])
    events = await collect(governor.handle_user_input("改支付回调签名校验"))
    dispatch_evs = _text_events_containing(events, "调度")
    assert "delivery" in dispatch_evs[0].data["text"]
    # 灌队列（seed 被调），不降级常驻（send 不被调，无降级事件）
    assert task_queue.seed.call_count == 1
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
# ── 9. forced_track="delivery" 跳过解析 ──
async def test_handle_forced_delivery():
    """forced_track='delivery' → _parse_requirement 不被调用，events 含 delivery+降级(无子任务)+send 事件"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(side_effect=AssertionError("不应调用"))
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])