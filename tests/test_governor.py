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
# ── 9. forced_track="delivery" 解析+强制 delivery(subtasks 非空) ──
async def test_handle_forced_delivery_runs_delivery(monkeypatch):
    """forced_track='delivery' -> 调 _parse_requirement;subtasks 非空 -> 强制走 _run_delivery"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(return_value={
        "task_summary": "x",
        "subtasks": [{"id": "t1", "domain": "backend", "summary": "s",
                      "acceptance": [], "module_id": "m", "deps": []}],
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
# ── 9b. forced_track="delivery" subtasks=[] -> 拦截不执行 ──
async def test_handle_forced_delivery_empty_subtasks_blocked(monkeypatch):
    """forced_track='delivery' + subtasks=[] -> 拦截提示,不走 _run_delivery"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(return_value={"task_summary": "x", "subtasks": []})
    async def fake_delivery(self, spec):
        yield text_event("should-not-happen")
    monkeypatch.setattr(Governor, "_run_delivery", fake_delivery)
    events = await collect(governor.handle_user_input("x", forced_track="delivery"))
    assert not _text_events_containing(events, "should-not-happen")
    assert _text_events_containing(events, "未拆出子任务")
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
    """_build_resident_session 挂载 plan_gen SDK server + tool_intercept；未配置 task_queue 也挂载"""
    governor = make_governor()
    assert governor._task_queue is None  # 未配置 TASK_SERVICE_DIR/TASK_DB
    session = governor._resident
    opts = session._build_options()
    assert "plan_gen" in opts.mcp_servers
    assert session.tool_intercept == governor._on_tool_use


async def test_capture_plan_handler():
    """propose_plan handler 截获 args 存 _pending_plan，返回确认文本"""
    governor = make_governor()
    spec = {"task_summary": "x", "subtasks": [{"id": "t1"}]}
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
        "harness_agent.core.base_session.BaseAgentSession.start", AsyncMock()
    )
    monkeypatch.setattr(
        "harness_agent.core.base_session.BaseAgentSession.close", AsyncMock()
    )
    governor = make_governor()
    governor._pending_plan = {"task_summary": "keep"}
    await governor.rebuild(model="other-model")
    opts = governor._resident._build_options()
    assert "plan_gen" in opts.mcp_servers
    assert governor._resident.tool_intercept == governor._on_tool_use
    assert governor._pending_plan == {"task_summary": "keep"}


# ── resident_plan：采纳闭环（采用方案 -> seed 灌队列）──
def _plan_spec():
    """带 2 子任务 + deps 的待采纳方案（propose_plan 截获格式）。"""
    return {
        "task_summary": "交付用户模块",
        "subtasks": [
            {
                "id": "t1",
                "domain": "backend",
                "module_id": "user",
                "summary": "建用户表",
                "acceptance": ["迁移可跑"],
                "deps": [],
            },
            {
                "id": "t2",
                "domain": "backend",
                "module_id": "user",
                "summary": "用户 API",
                "acceptance": ["测试通过"],
                "deps": ["t1"],
            },
        ],
    }


def _make_governor_with_queue():
    """Governor + MagicMock task_queue（seed 返回 tasks 数）。"""
    mq = MagicMock()
    mq.seed = MagicMock(side_effect=lambda tasks: len(tasks))
    governor = Governor(
        project_dir=".", session_store=MagicMock(spec=SessionStore), task_queue=mq
    )
    return governor, mq


async def test_adopt_plan_seeds_and_clears():
    """采用方案 + 待采纳方案 -> seed 灌队列（id/deps 映射）-> 反馈 req_id+数量 -> 清空防重复"""
    governor, mq = _make_governor_with_queue()
    governor._pending_plan = _plan_spec()
    events = await collect(governor.handle_user_input("采用方案"))
    # seed 一次，2 任务，id 带 req_id 前缀，deps 映射到前缀 id
    assert mq.seed.call_count == 1
    tasks = mq.seed.call_args.args[0]
    assert len(tasks) == 2
    req_id = tasks[0]["req_id"]
    assert tasks[0]["id"] == f"{req_id}-t1"
    assert tasks[1]["deps"] == [f"{req_id}-t1"]
    assert tasks[0]["module_id"] == "user"
    # 反馈事件含 req_id + 数量
    text = "\n".join(e.data.get("text", "") for e in events)
    assert req_id in text
    assert "已灌入 2 个任务" in text
    # 方案清空，防重复灌入
    assert governor._pending_plan is None
    # 再次采纳 -> 无待采纳提示，不再 seed
    events2 = await collect(governor.handle_user_input("采用方案"))
    assert mq.seed.call_count == 1
    assert "无待采纳方案" in events2[0].data["text"]


async def test_adopt_plan_no_pending():
    """无待采纳方案 -> 明确提示，不 seed"""
    governor, mq = _make_governor_with_queue()
    events = await collect(governor.handle_user_input("采用方案"))
    assert mq.seed.call_count == 0
    assert "无待采纳方案" in events[0].data["text"]


async def test_adopt_plan_no_task_queue():
    """task_queue=None -> 降级提示不崩溃，方案保留可重试"""
    governor = make_governor()  # 无 env -> _task_queue None
    governor._pending_plan = _plan_spec()
    events = await collect(governor.handle_user_input("采用方案"))
    assert "未配置任务队列" in events[0].data["text"]
    assert governor._pending_plan is not None


async def test_adopt_plan_empty_subtasks():
    """方案无子任务 -> 提示不 seed，方案保留"""
    governor, mq = _make_governor_with_queue()
    governor._pending_plan = {"task_summary": "x", "subtasks": []}
    events = await collect(governor.handle_user_input("采用方案"))
    assert mq.seed.call_count == 0
    assert "未拆出子任务" in events[0].data["text"]
    assert governor._pending_plan is not None


# ── resident_plan：采纳灌队列端到端（真实 sqlite TASK_DB）──
# 最小 store stub（契约同 task-service/store.py，仅 init_db+seed；
# adapter.list_by_req 直查 sqlite 不经 store，无需 stub）。
# 隔离 sys.path/sys.modules 污染（同 test_task_queue_adapter 套路）。
_MINI_STORE = '''\
import json
import sqlite3


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                req_id TEXT,
                domain TEXT,
                module_id TEXT,
                prompt TEXT,
                intended_files TEXT,
                deps TEXT,
                status TEXT DEFAULT 'pending',
                claimed_at TEXT,
                done_at TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def seed(tasks, db_path=None):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        for t in tasks:
            cur = conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (id, req_id, domain, module_id, prompt, intended_files, deps, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    t["id"],
                    t["req_id"],
                    t.get("domain", ""),
                    t.get("module_id"),
                    t.get("prompt", ""),
                    json.dumps(t.get("intended_files") or []),
                    json.dumps(t.get("deps") or []),
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted
'''


@pytest.fixture
def mini_store_dir(tmp_path):
    """写最小 store.py 到 tmp_path；teardown 清 sys.modules/sys.path 污染。"""
    import sys
    from pathlib import Path

    (tmp_path / "store.py").write_text(_MINI_STORE, encoding="utf-8")
    svc_dir = str(tmp_path)
    yield svc_dir
    sys.modules.pop("store", None)
    resolved = str(Path(svc_dir).resolve())
    for p in (svc_dir, resolved):
        while p in sys.path:
            sys.path.remove(p)


def _make_governor_real_db(svc_dir, db_path):
    """Governor + 真实 TaskQueueAdapter（tmp sqlite TASK_DB）。"""
    adapter = TaskQueueAdapter(svc_dir, db_path)
    return Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        task_queue=adapter,
    ), adapter


async def test_capture_then_adopt_real_sqlite(mini_store_dir, tmp_path):
    """工具调用截获待采纳方案 -> 采用方案 -> sqlite 可 list_by_req 查到，字段/deps 映射正确"""
    import re

    db_path = str(tmp_path / "task.db")
    governor, adapter = _make_governor_real_db(mini_store_dir, db_path)
    # 模拟方案工具调用 -> Governor 暂存待采纳方案
    await governor._capture_plan(_plan_spec())
    assert governor._pending_plan is not None
    assert governor._pending_plan["task_summary"] == "交付用户模块"
    # 采纳确认
    events = await collect(governor.handle_user_input("采用方案"))
    text = "\n".join(e.data.get("text", "") for e in events)
    m = re.search(r"req_id=([0-9a-f]{8})", text)
    assert m, f"反馈事件缺 req_id: {text}"
    req_id = m.group(1)
    # sqlite 直查：2 任务，字段/deps 映射正确
    rows = adapter.list_by_req(req_id)
    assert len(rows) == 2
    assert [r["id"] for r in rows] == [f"{req_id}-t1", f"{req_id}-t2"]
    assert all(r["domain"] == "backend" for r in rows)
    assert all(r["module_id"] == "user" for r in rows)
    assert all(r["status"] == "pending" for r in rows)
    assert json.loads(rows[0]["deps"]) == []
    assert json.loads(rows[1]["deps"]) == [f"{req_id}-t1"]
    # 方案已清空
    assert governor._pending_plan is None


async def test_adopt_no_pending_no_db_write(mini_store_dir, tmp_path):
    """无待采纳方案 -> 提示且不写库（DB 文件不创建）"""
    from pathlib import Path

    db_path = str(tmp_path / "task.db")
    governor, adapter = _make_governor_real_db(mini_store_dir, db_path)
    events = await collect(governor.handle_user_input("采用方案"))
    assert "无待采纳方案" in events[0].data["text"]
    assert not Path(db_path).exists()
