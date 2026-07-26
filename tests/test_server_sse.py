"""端到端 SSE 冒烟: 真实 uvicorn server + httpx SSE 客户端.

上轮发现 httpx ASGITransport 无法测无限 SSE 流(须 await 整个 app 完成),
故这里起真实 uvicorn server, 用独立 httpx client 连 GET /events 保持长连接,
另一 client POST /chat, 验证完整事件序列实时推送.

复用 tests.test_server_app.FakeSession(duck-typed ChatSession, 不拉 SDK 子进程).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx
import pytest
import uvicorn

from autoloop_agent.server import create_app
from tests.test_server_app import FakeSession


async def _wait_started(server: uvicorn.Server, timeout: float = 5.0) -> int:
    """等 uvicorn 监听就绪, 返回实际绑定端口(port=0 时由 OS 分配)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not server.started:
        if loop.time() > deadline:
            raise RuntimeError("uvicorn server 未在超时内启动")
        await asyncio.sleep(0.05)
    sock = server.servers[0].sockets[0]
    return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_sse_end_to_end_full_event_sequence():
    """POST /chat + GET /events 收到完整事件序列.

    流程: 先 GET /events 建立订阅(长连接) -> POST /chat 触发 send()
    -> 从 SSE 流实时读 turn_start/text/usage/turn_end 四条事件.
    """
    sess = FakeSession()
    app = create_app(session=sess)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        port = await _wait_started(server)
        base = f"http://127.0.0.1:{port}"

        async with httpx.AsyncClient(base_url=base) as sse_client, \
                httpx.AsyncClient(base_url=base) as chat_client:

            async def consume_sse() -> list[dict]:
                async with sse_client.stream("GET", "/events") as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]
                    events: list[dict] = []
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            events.append(json.loads(line[len("data: "):]))
                            if events[-1]["type"] == "turn_end":
                                break
                    return events

            consumer = asyncio.create_task(consume_sse())
            # 等 SSE 连接建立 + subscribe 注册队列
            await asyncio.sleep(0.2)

            chat_resp = await chat_client.post("/chat", json={"prompt": "hello"})
            assert chat_resp.status_code == 200
            ack = chat_resp.json()
            assert ack["status"] == "accepted"
            request_id = ack["request_id"]

            events = await asyncio.wait_for(consumer, timeout=10)

        # ── 完整事件序列 ──
        types = [e["type"] for e in events]
        assert types == ["turn_start", "text", "usage", "turn_end"], types

        # ── request_id 归属一致(同一请求所有事件共享) ──
        assert all(e["request_id"] == request_id for e in events), \
            {e["type"]: e["request_id"] for e in events}

        # ── 协议字段齐全 + 类型 ──
        for e in events:
            assert set(e.keys()) == {"type", "data", "agent", "request_id", "timestamp"}
            assert isinstance(e["data"], dict)
            assert e["agent"] == "default"
            # timestamp 走 isoformat, 可还原
            datetime.fromisoformat(e["timestamp"])

        # ── 具体 data 内容 ──
        assert events[0]["data"]["prompt"] == "hello"
        assert events[0]["data"]["turn_number"] == 1
        assert events[1]["data"]["text"] == "hello"
        assert events[2]["data"]["input_tokens"] == 10
        assert events[2]["data"]["output_tokens"] == 5
        assert events[3]["data"]["tool_count"] == 0

    finally:
        server.should_exit = True
        await server_task
