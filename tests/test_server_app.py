"""server 适配层测试 - ServerSession + create_app.

覆盖验收:
- POST /chat 触发 send(), 立即返回 ack 不阻塞
- GET /events 从 asyncio.Queue 实时推送序列化 ChatEvent
- asyncio.Lock 串行化并发 send
- POST /cancel 中断进行中请求
- GET /status 暴露 SessionStats
- server 复用 ChatSession 构造, 不改 BaseAgentSession.send() 签名
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest

from autoloop_agent.chat.events import (
    ChatEvent,
    EventType,
    cancelled_event,
    text_event,
    turn_end_event,
    turn_start_event,
    usage_event,
)
from autoloop_agent.server import ServerSession, create_app


# ── FakeSession(duck-typed ChatSession, 不拉 SDK 子进程)──


class FakeStats:
    def __init__(self) -> None:
        self.turn_count = 0
        self.total_input_tokens = 10
        self.total_output_tokens = 5
        self.total_cost_usd = 0.01

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": self.total_cost_usd,
        }


class FakeSession:
    """duck-typed ChatSession: send/cancel/start/close + stats."""

    def __init__(self) -> None:
        self.stats = FakeStats()
        self.cancelled = False
        self.last_prompt: str | None = None
        self.send_started = asyncio.Event()
        self.send_finished = asyncio.Event()
        # 闸门: send 在此等待, 便于测试控制时序(锁串行 / cancel)
        self.gate = asyncio.Event()
        self.gate.set()  # 默认放行

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    async def send(self, prompt: str) -> AsyncIterator[ChatEvent]:
        self.last_prompt = prompt
        self.send_started.set()
        yield turn_start_event(1, prompt)
        await self.gate.wait()
        if self.cancelled:
            yield cancelled_event("用户取消了请求")
            return
        self.stats.turn_count += 1
        yield text_event("hello", request_id=None)
        yield usage_event(input_tokens=10, output_tokens=5)
        yield turn_end_event(1, tool_count=0, duration_ms=10)
        self.send_finished.set()


# ── ServerSession 单元测试 ──────────────────────────────


@pytest.mark.asyncio
async def test_submit_returns_request_id_and_broadcasts():
    """POST /chat 等价: submit 立即返回 request_id, 事件广播到订阅队列."""
    sess = FakeSession()
    state = ServerSession(sess)
    q = state.subscribe()

    rid = state.submit("hi")
    assert isinstance(rid, str) and len(rid) == 8

    # 立即返回不阻塞: submit 时 send 可能尚未 started
    # 等待事件流: turn_start -> text -> usage -> turn_end
    events: list[ChatEvent] = []
    for _ in range(4):
        events.append(await asyncio.wait_for(q.get(), timeout=2))

    types = [e.type for e in events]
    assert types == [
        EventType.TURN_START,
        EventType.TEXT,
        EventType.USAGE,
        EventType.TURN_END,
    ]
    # 所有事件打上归属 request_id
    assert all(e.request_id == rid for e in events)
    # send 收到 prompt
    assert sess.last_prompt == "hi"
    # 任务完成后清理
    await asyncio.wait_for(sess.send_finished.wait(), timeout=2)
    assert state._current_request_id is None


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_send():
    """asyncio.Lock 串行化: 第二个 send 必须等第一个结束才开始."""
    sess = FakeSession()
    sess.gate.clear()  # send 阻塞在 gate
    state = ServerSession(sess)

    rid1 = state.submit("first")
    await asyncio.wait_for(sess.send_started.wait(), timeout=2)
    # 第一个还在跑(门关着), 提交第二个
    rid2 = state.submit("second")
    # 给第二个一点机会: 它不该 start(send_started 已被 first set, 复用会误判)
    # 用 stats.turn_count 间接判定: 第二个没跑完
    await asyncio.sleep(0.05)
    assert sess.last_prompt == "first"  # 仍是第一个的 prompt

    # 放行第一个
    sess.gate.set()
    await asyncio.wait_for(sess.send_finished.wait(), timeout=2)
    # 第一个结束后第二个接管
    # 第二个 send 会复用同一 gate(已 set), 直接跑完
    # 等第二个完成: 订阅队列会收到 rid2 的事件
    q = state.subscribe()
    # drain rid2 的事件(turn_start..turn_end)
    rid2_events = []
    deadline = asyncio.get_event_loop().time() + 2
    while len(rid2_events) < 4:
        if asyncio.get_event_loop().time() > deadline:
            break
        try:
            ev = await asyncio.wait_for(q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if ev.request_id == rid2:
            rid2_events.append(ev)
    assert len(rid2_events) == 4
    assert sess.last_prompt == "second"


@pytest.mark.asyncio
async def test_cancel_interrupts_active_request():
    """POST /cancel: 调用 session.cancel(), 中断进行中请求."""
    sess = FakeSession()
    sess.gate.clear()
    state = ServerSession(sess)
    q = state.subscribe()

    rid = state.submit("work")
    await asyncio.wait_for(sess.send_started.wait(), timeout=2)
    assert state.cancel() is True
    assert sess.cancelled is True
    sess.gate.set()  # 放行 send, 它会检查 cancelled

    # 收到 turn_start + cancelled
    ev1 = await asyncio.wait_for(q.get(), timeout=2)
    assert ev1.type == EventType.TURN_START
    ev2 = await asyncio.wait_for(q.get(), timeout=2)
    assert ev2.type == EventType.CANCELLED
    assert ev2.request_id == rid

    # 让 _run 收尾(cancelled 路径不 set send_finished, 用短等待)
    await asyncio.sleep(0.05)
    assert state._current_request_id is None


@pytest.mark.asyncio
async def test_cancel_idle_returns_false():
    """无活动请求时 cancel 返回 False(idle)."""
    state = ServerSession(FakeSession())
    assert state.cancel() is False


@pytest.mark.asyncio
async def test_status_returns_stats_dict():
    """GET /status: 暴露 SessionStats.to_dict()."""
    sess = FakeSession()
    state = ServerSession(sess)
    s = state.status()
    assert s["turn_count"] == 0
    assert s["total_input_tokens"] == 10
    assert s["total_cost_usd"] == 0.01
    assert set(s.keys()) >= {"turn_count", "total_input_tokens", "total_output_tokens"}


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_isolation():
    """订阅者互不干扰, unsubscribe 后不再收到事件."""
    sess = FakeSession()
    state = ServerSession(sess)
    q1 = state.subscribe()
    q2 = state.subscribe()
    state.unsubscribe(q1)

    state.submit("hi")
    # q2 收到, q1 不应收到(已注销)
    ev = await asyncio.wait_for(q2.get(), timeout=2)
    assert ev.type == EventType.TURN_START
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q1.get(), timeout=0.2)

    # drain q2 剩余事件让后台任务结束
    for _ in range(3):
        await asyncio.wait_for(q2.get(), timeout=2)


# ── create_app HTTP 层冒烟 ──────────────────────────────


def test_create_app_registers_routes():
    """create_app 注册 /chat /events /cancel /status 四条路由, 复用 ChatSession 构造."""
    app = create_app(session=FakeSession())
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/chat", "/events", "/cancel", "/status"} <= paths


@pytest.mark.asyncio
async def test_app_status_endpoint():
    """GET /status 经 HTTP 返回 SessionStats dict."""
    import httpx

    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["turn_count"] == 0
    assert body["total_cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_app_chat_returns_ack_immediately():
    """POST /chat 立即返回 ack{status, request_id}, 不阻塞等 send 完成."""
    import httpx

    sess = FakeSession()
    sess.gate.clear()  # send 阻塞, 证明 ack 不等 send
    app = create_app(session=sess)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/chat", json={"prompt": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert isinstance(body["request_id"], str)
        # ack 已返回但 send 仍在 gate 上阻塞(未完成)
        await asyncio.sleep(0.05)  # 让 bg 任务起步到 gate
        assert sess.send_started.is_set()
        assert not sess.send_finished.is_set()
        # 放行让后台 send 跑完, 避免 lifespan 关闭时悬挂
        sess.gate.set()
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_app_cancel_idle():
    """POST /cancel 无活动请求 -> idle."""
    import httpx

    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/cancel")
    assert r.status_code == 200
    assert r.json() == {"status": "idle"}


@pytest.mark.asyncio
async def test_app_cancel_active_request():
    """POST /chat 后 POST /cancel -> cancelling, 且 session.cancel 被调用."""
    import httpx

    sess = FakeSession()
    sess.gate.clear()  # send 阻塞, 保持请求活动
    app = create_app(session=sess)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/chat", json={"prompt": "work"})
        await asyncio.wait_for(sess.send_started.wait(), timeout=2)
        r = await client.post("/cancel")
        assert r.status_code == 200
        assert r.json() == {"status": "cancelling"}
        assert sess.cancelled is True
        sess.gate.set()
        await asyncio.sleep(0.1)


def test_app_events_route_registered():
    """GET /events 路由注册(端到端 SSE 流消费无法用 httpx ASGITransport 测试:
    其 handle_async_request 须 await 整个 app 完成, 无限 SSE 流永不结束 -> 挂起).

    "从 asyncio.Queue 推送序列化 ChatEvent" 在 ServerSession 层覆盖:
      - test_submit_returns_request_id_and_broadcasts
      - test_subscribe_unsubscribe_isolation
    序列化往返见 test_chat_events.py.
    """
    app = create_app(session=FakeSession())
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/events" in paths

