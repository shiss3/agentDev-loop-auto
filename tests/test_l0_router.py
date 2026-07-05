"""L0Router 单元测试 — Phase 2 Step 2

覆盖：
- _route: stateless query() 路由判定（structured_output / RuntimeError）
- handle_user_input: 自动路由 / forced_lane / 异常降级
- _lane_guard: 动态权限矩阵（fast/full/None × 业务写/Bash/Read）
- _build_resident_session: 配置接线
- session property / rebuild / 入口重置 lane
- _route 调用 query 顶层 API 的参数契约

所有测试不拉真实 SDK/CLI/文件系统：
- session_store 用 MagicMock(spec=SessionStore) 避免 ~/.harness/sessions 真实 mkdir
- query / _session.send / _route 用 fake 替换
- BaseAgentSession.start/close 在 rebuild 测试中 patch 为 AsyncMock
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SessionStore,
)

from harness_agent.chat.events import EventType, text_event
from harness_agent.core import architect
from harness_agent.core.architect import EXECUTOR_PROMPT, L0Router
from harness_agent.core.base_session import BaseAgentSession


# ── 辅助 ──────────────────────────────────────────────


def make_fake_result_msg(structured_output):
    """构造能通过 isinstance(msg, ResultMessage) 检查的 fake 消息。

    MagicMock(spec=ResultMessage) —— spec 让 isinstance 返回 True。
    """
    msg = MagicMock(spec=ResultMessage)
    msg.structured_output = structured_output
    return msg


def make_fake_send(events):
    """构造 fake async generator send（替换 _session.send）。

    send 是 async generator（`async for event in send(...)`），fake 也必须是。
    返回的 fake_send 可多次调用，每次产出全新生成器。
    """

    async def fake_send(prompt):
        for ev in events:
            yield ev

    return fake_send


def make_router():
    """构造 L0Router，session_store 用 mock 避免真实文件 mkdir。"""
    return L0Router(project_dir=".", session_store=MagicMock(spec=SessionStore))


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
    """返回 setter：传 structured_output，patch architect.query 返回它。

    用法：
        patch_query({"lane": "fast", "reason": "r", "tasks": []})
        decision = await router._route("x")
    """

    def _set(structured_output):
        async def fake_query(*, prompt, options=None):
            yield make_fake_result_msg(structured_output)

        monkeypatch.setattr(architect, "query", fake_query)

    return _set


# ── 1. _route 返回 structured_output ──


async def test_route_returns_structured_output(patch_query):
    """patch_query 返回 fast 决策 → _route 返回该 dict"""
    router = make_router()
    expected = {"lane": "fast", "reason": "r", "tasks": []}
    patch_query(expected)

    decision = await router._route("x")

    assert decision == expected


# ── 2. _route 无 structured_output → RuntimeError ──


async def test_route_no_structured_output_raises(patch_query):
    """query 返回 structured_output=None → _route raise RuntimeError"""
    router = make_router()
    patch_query(None)

    with pytest.raises(RuntimeError, match="未返回结构化结果"):
        await router._route("x")


# ── 3. handle_user_input 自动 fast ──


async def test_handle_fast_lane_auto():
    """mock _route→fast + mock _session.send → events 含路由判定 + send 事件，lane=fast"""
    router = make_router()
    router._route = AsyncMock(
        return_value={"lane": "fast", "reason": "r", "tasks": []}
    )
    fake_ev = text_event("fake-exec")
    router._session.send = make_fake_send([fake_ev])

    events = await collect(router.handle_user_input("x"))

    route_evs = _text_events_containing(events, "路由判定")
    assert len(route_evs) == 1
    assert "fast" in route_evs[0].data["text"]
    assert fake_ev in events
    assert router._current_lane == "fast"


# ── 4. handle_user_input full 占位 ──


async def test_handle_full_lane_placeholder():
    """mock _route→full+tasks → events 含路由判定: full + 占位消息(Step 4)，lane=full"""
    router = make_router()
    router._route = AsyncMock(
        return_value={
            "lane": "full",
            "reason": "r",
            "tasks": [{"domain": "a", "prompt": "b"}],
        }
    )

    events = await collect(router.handle_user_input("x"))

    route_evs = _text_events_containing(events, "路由判定")
    assert len(route_evs) == 1
    assert "full" in route_evs[0].data["text"]
    placeholder_evs = _text_events_containing(events, "Step 4")
    assert len(placeholder_evs) == 1
    assert router._current_lane == "full"


# ── 5. forced_lane="fast" 跳过 _route ──


async def test_handle_forced_fast_skips_route():
    """forced_lane='fast' → _route 不被调用，events 含'强制车道: fast' + send 事件"""
    router = make_router()
    router._route = AsyncMock(side_effect=AssertionError("不应调用"))
    fake_ev = text_event("fake-exec")
    router._session.send = make_fake_send([fake_ev])

    events = await collect(router.handle_user_input("x", forced_lane="fast"))

    forced_evs = _text_events_containing(events, "强制车道")
    assert len(forced_evs) == 1
    assert "fast" in forced_evs[0].data["text"]
    assert fake_ev in events
    router._route.assert_not_called()
    assert router._current_lane == "fast"


# ── 6. forced_lane="full" 覆盖 lane ──


async def test_handle_forced_full_overrides_lane():
    """forced_lane='full' + _route 返回 fast → lane 被覆盖为 full，events 含强制车道+占位"""
    router = make_router()
    router._route = AsyncMock(
        return_value={
            "lane": "fast",
            "reason": "r",
            "tasks": [{"domain": "a", "prompt": "b"}],
        }
    )

    events = await collect(router.handle_user_input("x", forced_lane="full"))

    forced_evs = _text_events_containing(events, "强制车道")
    assert len(forced_evs) == 1
    assert "full" in forced_evs[0].data["text"]
    placeholder_evs = _text_events_containing(events, "Step 4")
    assert len(placeholder_evs) == 1
    assert router._current_lane == "full"


# ── 7. _route 异常 → 降级 fast ──


async def test_handle_route_exception_degrades_to_fast():
    """_route raise RuntimeError('boom') → 降级 fast + send 事件"""
    router = make_router()
    router._route = AsyncMock(side_effect=RuntimeError("boom"))
    fake_ev = text_event("fake-exec")
    router._session.send = make_fake_send([fake_ev])

    events = await collect(router.handle_user_input("x"))

    degrade_evs = _text_events_containing(events, "降级")
    assert len(degrade_evs) == 1
    assert fake_ev in events
    assert router._current_lane == "fast"


# ── 8. _lane_guard fast 放行业务写 ──


async def test_lane_guard_fast_allows_business_write():
    """fast lane → Write 放行"""
    router = make_router()
    router._current_lane = "fast"

    result = await router._lane_guard("Write", {}, None)

    assert isinstance(result, PermissionResultAllow)


# ── 9. _lane_guard full 拒绝业务写 ──


async def test_lane_guard_full_denies_business_write():
    """full lane → Write 拒绝"""
    router = make_router()
    router._current_lane = "full"

    result = await router._lane_guard("Write", {}, None)

    assert isinstance(result, PermissionResultDeny)


# ── 10. _lane_guard None 拒绝业务写 ──


async def test_lane_guard_none_denies_business_write():
    """lane=None → Edit 拒绝（异常残留保护）"""
    router = make_router()
    router._current_lane = None

    result = await router._lane_guard("Edit", {}, None)

    assert isinstance(result, PermissionResultDeny)


# ── 11. _lane_guard 任意 lane 放行 Bash ──


async def test_lane_guard_allows_bash_any_lane():
    """full lane → Bash 放行（运维工具始终允许）"""
    router = make_router()
    router._current_lane = "full"

    result = await router._lane_guard("Bash", {}, None)

    assert isinstance(result, PermissionResultAllow)


# ── 12. _lane_guard 任意 lane 放行 Read ──


async def test_lane_guard_allows_read_any_lane():
    """full lane → Read 放行（只读始终允许）"""
    router = make_router()
    router._current_lane = "full"

    result = await router._lane_guard("Read", {}, None)

    assert isinstance(result, PermissionResultAllow)


# ── 13. 入口重置 lane ──


async def test_entrance_resets_lane():
    """第二次 handle_user_input（自动路由）入口把 _current_lane 重置为 None

    时序：
    1. 第一次 forced_lane='fast' → _current_lane='fast'
    2. 第二次 forced_lane=None → 入口先 _current_lane=None 再调 _route
       spy 在 _route 调用时断言 _current_lane is None
    """
    router = make_router()
    router._session.send = make_fake_send([text_event("fake")])

    # 第一次：设 lane=fast
    await collect(router.handle_user_input("a", forced_lane="fast"))
    assert router._current_lane == "fast"

    # 第二次：自动路由，spy 检查入口是否重置
    async def spy_route(text):
        assert router._current_lane is None, "入口未重置 _current_lane"
        return {"lane": "fast", "reason": "r", "tasks": []}

    router._route = spy_route
    await collect(router.handle_user_input("b"))
    # spy 断言通过即测试通过


# ── 14. _build_resident_session 配置 ──


def test_build_resident_session_config():
    """常驻 session 配置：permission_mode=default / 业务写不在 allowed_tools /
    Bash 在 / can_use_tool 已挂载 / system_prompt=EXECUTOR_PROMPT"""
    router = L0Router(".", session_store=MagicMock(spec=SessionStore))
    sess = router._session
    assert sess.permission_mode == "default"
    assert sess.can_use_tool is not None
    assert "Write" not in sess.allowed_tools
    assert "Edit" not in sess.allowed_tools
    assert "NotebookEdit" not in sess.allowed_tools
    assert "Bash" in sess.allowed_tools
    assert sess.system_prompt == EXECUTOR_PROMPT


# ── 15. session property ──


def test_session_property():
    """router.session is router._session（穿透到常驻底座）"""
    router = make_router()
    assert router.session is router._session


# ── 16. rebuild 创建新 session ──


async def test_rebuild_creates_new_session(monkeypatch):
    """rebuild → close 旧 session + 创建新 session + start"""
    monkeypatch.setattr(BaseAgentSession, "start", AsyncMock())
    monkeypatch.setattr(BaseAgentSession, "close", AsyncMock())
    router = make_router()
    old = router._session

    await router.rebuild()

    assert router._session is not old
    # close 被调一次（旧 session）
    assert old.close.await_count == 1
    # start 被调一次（新 session）
    assert router._session.start.await_count == 1


# ── 17. _route 用 query 顶层 API 的参数契约 ──


async def test_route_uses_query_top_level_api(monkeypatch):
    """_route 调 architect.query，关键字参数 prompt/options，
    options.tools=[] / output_format=json_schema / max_turns=3"""
    router = make_router()

    async def fake_query(*, prompt, options=None):
        yield make_fake_result_msg({"lane": "fast", "reason": "r", "tasks": []})

    mock_query = MagicMock(side_effect=fake_query)
    monkeypatch.setattr(architect, "query", mock_query)

    await router._route("x")

    assert mock_query.call_count == 1
    kwargs = mock_query.call_args.kwargs
    assert kwargs["prompt"] == "x"
    opts = kwargs["options"]
    assert opts.tools == []
    assert opts.output_format is not None
    assert opts.output_format["type"] == "json_schema"
    assert opts.max_turns == 3


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
