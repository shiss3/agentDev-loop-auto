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

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ResultMessage, SessionStore

from harness_agent.chat.events import EventType, text_event
from harness_agent.core import architect
from harness_agent.core.architect import EXECUTOR_PROMPT, Governor, TASK_SPEC_SCHEMA
from harness_agent.core.base_session import BaseAgentSession
from harness_agent.core.task_queue_adapter import TaskQueueAdapter


# ── 辅助 ──────────────────────────────────────────────


def make_fake_result_msg(structured=None):
    """构造能通过 isinstance(msg, ResultMessage) 检查的 fake 消息。

    MagicMock(spec=ResultMessage) —— spec 让 isinstance 返回 True。
    structured: 填 msg.structured_output(结构化输出 dict;None 表示无)。
    """
    msg = MagicMock(spec=ResultMessage)
    msg.structured_output = structured
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

    def _set(structured):
        async def fake_query(*, prompt, options=None):
            yield make_fake_result_msg(structured)

        monkeypatch.setattr(architect, "query", fake_query)

    return _set


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


# ── 2. _parse_requirement 无 JSON 文本 → RuntimeError ──


async def test_parse_requirement_no_output_raises(patch_query):
    """query 返回无 structured_output -> _parse_requirement raise RuntimeError"""
    governor = make_governor()
    patch_query(None)  # 无结构化输出

    with pytest.raises(RuntimeError, match="未返回结构化结果"):
        await governor._parse_requirement("x", context_continuation=False)



# ── 3. _decide_track 全满足(patch+1-3+cross_domain=False) -> interactive ──


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


async def test_handle_delivery_placeholder():
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
            "file_count_bucket": "4+",
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

    events = await collect(governor.handle_user_input("x", forced_track="delivery"))

    dispatch_evs = _text_events_containing(events, "调度")
    assert len(dispatch_evs) == 1
    assert "delivery" in dispatch_evs[0].data["text"]
    degrade_evs = _text_events_containing(events, "无子任务")
    assert len(degrade_evs) == 1
    assert fake_ev in events
    governor._parse_requirement.assert_not_called()


# ── 10. _parse_requirement 异常 → 降级交互轨 ──


async def test_handle_parse_exception_degrades():
    """_parse_requirement raise RuntimeError('boom') → 降级交互轨 + send 事件"""
    governor = make_governor()
    governor._parse_requirement = AsyncMock(side_effect=RuntimeError("boom"))
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])

    events = await collect(governor.handle_user_input("x"))

    degrade_evs = _text_events_containing(events, "降级")
    assert len(degrade_evs) == 1
    assert fake_ev in events


# ── 11. _build_resident_session 配置 ──


def test_build_resident_session_config():
    """常驻执行体配置：permission_mode=acceptEdits / 业务写在 allowed_tools /
    Bash 在 / can_use_tool 未挂载 / system_prompt=EXECUTOR_PROMPT"""
    governor = Governor(".", session_store=MagicMock(spec=SessionStore))
    sess = governor._resident
    assert sess.permission_mode == "acceptEdits"
    assert sess.can_use_tool is None
    assert "Write" in sess.allowed_tools
    assert "Edit" in sess.allowed_tools
    assert "Bash" in sess.allowed_tools
    assert sess.system_prompt == EXECUTOR_PROMPT


# ── 12. session property ──


def test_session_property():
    """governor.session is governor._resident（穿透到常驻执行体）"""
    governor = make_governor()
    assert governor.session is governor._resident


# ── 13. rebuild 创建新 session ──


async def test_rebuild_creates_new_session(monkeypatch):
    """rebuild → close 旧执行体 + 创建新执行体 + start"""
    monkeypatch.setattr(BaseAgentSession, "start", AsyncMock())
    monkeypatch.setattr(BaseAgentSession, "close", AsyncMock())
    governor = make_governor()
    old = governor._resident

    await governor.rebuild()

    assert governor._resident is not old
    # close 被调一次（旧执行体）
    assert old.close.await_count == 1
    # start 被调一次（新执行体）
    assert governor._resident.start.await_count == 1


# ── 14. _parse_requirement 用 query 顶层 API 的参数契约 ──


async def test_parse_requirement_uses_query_top_level_api(monkeypatch):
    """_parse_requirement 调 architect.query，关键字参数 prompt/options，
    options.tools=[] / strict_mcp_config=True / output_format=json_schema / max_turns=2"""
    governor = make_governor()

    async def fake_query(*, prompt, options=None):
        yield make_fake_result_msg(
            {"task_summary": "s", "acceptance_criteria": [], "risk_level": "low", "suggest_track": "interactive"}
        )

    mock_query = MagicMock(side_effect=fake_query)
    monkeypatch.setattr(architect, "query", mock_query)

    await governor._parse_requirement("x", context_continuation=False)

    assert mock_query.call_count == 1
    kwargs = mock_query.call_args.kwargs
    assert "x" in kwargs["prompt"]  # prompt 含原始 text（带 [全新任务] 前缀）
    opts = kwargs["options"]
    assert opts.tools == []
    assert opts.output_format == {"type": "json_schema", "schema": TASK_SPEC_SCHEMA}
    assert opts.strict_mcp_config is True  # 禁外部 MCP,防工具注入耗尽 turn
    assert opts.max_turns == 2


# ── 15. _parse_requirement 传递 context_continuation 前缀 ──


async def test_parse_requirement_context_continuation_prefix(monkeypatch):
    """context_continuation=True → prompt 含 [接续修改] 前缀；
    context_continuation=False → prompt 含 [全新任务] 前缀"""
    governor = make_governor()
    captured_prompts = []

    async def fake_query(*, prompt, options=None):
        captured_prompts.append(prompt)
        yield make_fake_result_msg(
            {"task_summary": "s", "acceptance_criteria": [], "risk_level": "low", "suggest_track": "interactive"}
        )

    monkeypatch.setattr(architect, "query", fake_query)

    await governor._parse_requirement("需求A", context_continuation=False)
    await governor._parse_requirement("需求B", context_continuation=True)

    assert "[全新任务]" in captured_prompts[0]
    assert "需求A" in captured_prompts[0]
    assert "[接续修改]" in captured_prompts[1]
    assert "需求B" in captured_prompts[1]


# ── 16. _run_delivery 无 subtasks -> 降级交互轨 ──


async def test_run_delivery_no_subtasks_degrades():
    """_run_delivery: spec subtasks 为空 -> yield 降级提示 + _resident.send 被调"""
    governor = make_governor()
    governor._last_text = "重构支付"
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])

    spec = {
        "task_summary": "重构支付",
        "suggest_track": "delivery",
        "subtasks": [],
    }
    events = await collect(governor._run_delivery(spec))

    degrade_evs = _text_events_containing(events, "无子任务")
    assert len(degrade_evs) == 1
    assert fake_ev in events


# ── 17. _run_delivery 有 subtasks 但无 queue -> 降级交互轨 ──


async def test_run_delivery_no_queue_degrades(monkeypatch):
    """_run_delivery: 有 subtasks 但 task_queue=None -> yield 降级提示 + send 被调"""
    monkeypatch.delenv("TASK_SERVICE_DIR", raising=False)
    monkeypatch.delenv("TASK_DB", raising=False)
    governor = make_governor()
    assert governor._task_queue is None
    governor._last_text = "重构支付"
    fake_ev = text_event("fake-exec")
    governor._resident.send = make_fake_send([fake_ev])

    spec = {
        "task_summary": "重构支付",
        "suggest_track": "delivery",
        "subtasks": [
            {"id": "t1", "domain": "backend", "module_id": "core", "summary": "建表", "acceptance": ["表存在"]}
        ],
    }
    events = await collect(governor._run_delivery(spec))

    degrade_evs = _text_events_containing(events, "未配置")
    assert len(degrade_evs) == 1
    assert fake_ev in events


# ── 18. _run_delivery 返回 req_id（8 hex + 一致性）──


async def test_run_delivery_returns_req_id():
    """_run_delivery: 有 subtasks+有 queue -> yield 行含 req_id（8 hex），
    tasks 植入的 id 形如 {req_id}-{short_id}，req_id 字段一致"""
    task_queue = MagicMock(spec=TaskQueueAdapter)
    task_queue.seed.return_value = 2
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        task_queue=task_queue,
    )
    spec = {
        "task_summary": "重构支付",
        "suggest_track": "delivery",
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
    events = await collect(governor._run_delivery(spec))

    req_id_evs = _text_events_containing(events, "req_id=")
    assert len(req_id_evs) == 1
    text = req_id_evs[0].data["text"]
    m = re.search(r"req_id=([0-9a-f]{8})", text)
    assert m, f"未找到 8 字符 hex req_id: {text}"
    req_id = m.group(1)

    seeded = task_queue.seed.call_args.args[0]
    for t in seeded:
        assert t["id"].startswith(f"{req_id}-")
        assert t["req_id"] == req_id
        assert t["module_id"] == "payment"


# ── 19. _build_task_prompt 自包含 ──


def test_build_task_prompt_self_contained():
    """_build_task_prompt: 输出含 summary + 每条 acceptance + intended_files +
    模块/领域行，不含上下文延续依赖词"""
    subtask = {
        "id": "t1",
        "domain": "backend",
        "module_id": "payment",
        "summary": "建支付表",
        "acceptance": ["表存在", "含金额字段"],
        "intended_files": ["db.sql", "migrate.py"],
        "deps": [],
    }
    spec = {
        "task_summary": "重构支付链路",
        "acceptance_criteria": ["支付链路全通"],
        "risk_level": "high",
        "suggest_track": "delivery",
    }
    prompt = Governor._build_task_prompt(subtask, spec)

    assert "建支付表" in prompt
    assert "表存在" in prompt
    assert "含金额字段" in prompt
    assert "db.sql" in prompt
    assert "migrate.py" in prompt
    assert "重构支付链路" in prompt
    assert "payment/backend" in prompt
    assert "context_continuation" not in prompt
    assert "上文" not in prompt


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
