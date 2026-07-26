"""FastAPI + asyncio.Queue + Lock 适配层.

不改动 BaseAgentSession.send() 签名, 仅消费其 AsyncIterator[ChatEvent].
ServerSession 持有:
  - chat: ChatSession(或 duck-typed fake, 便于测试注入)
  - _lock: asyncio.Lock 串行化并发 send(单会话状态防撞车)
  - _subscribers: set[asyncio.Queue] - GET /events 每个连接一个队列,
                  POST /chat 产出的事件 fan-out 到所有订阅者
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from autoloop_agent.chat.events import ChatEvent, error_event
from autoloop_agent.chat.session import ChatSession

logger = logging.getLogger(__name__)


class ServerSession:
    """单会话状态: ChatSession + asyncio.Lock + 订阅者广播."""

    def __init__(self, chat: Any) -> None:
        # chat duck-typed: 需 send/cancel/start/close + stats.to_dict()
        # 真实路径为 ChatSession; 测试可注入 fake.
        self.chat = chat
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[ChatEvent]] = set()
        self._current_request_id: str | None = None
        self._current_task: asyncio.Task[None] | None = None

    # ── 订阅 / 广播 ──────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[ChatEvent]:
        """注册一个订阅队列(GET /events 连接时调用)."""
        q: asyncio.Queue[ChatEvent] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ChatEvent]) -> None:
        """注销订阅(连接断开时调用)."""
        self._subscribers.discard(q)

    def _broadcast(self, event: ChatEvent) -> None:
        for q in list(self._subscribers):
            q.put_nowait(event)

    # ── 请求 ─────────────────────────────────────────────

    def submit(self, prompt: str) -> str:
        """非阻塞提交: 后台拿锁跑 send(), 立即返回 request_id.

        Lock 串行化: 并发 submit 排队执行, 防同会话状态撞车.
        POST /chat 立即返回 ack, 不等待 send 完成.
        """
        request_id = uuid.uuid4().hex[:8]
        self._current_request_id = request_id
        self._current_task = asyncio.create_task(self._run(request_id, prompt))
        return request_id

    async def _run(self, request_id: str, prompt: str) -> None:
        task = asyncio.current_task()
        async with self._lock:
            try:
                async for event in self.chat.send(prompt):
                    # send 产出的事件 request_id 默认 None, 在此打标归属
                    if event.request_id is None:
                        event.request_id = request_id
                    self._broadcast(event)
            except Exception as e:
                logger.exception("session.send failed (request_id=%s)", request_id)
                self._broadcast(error_event(str(e), request_id=request_id))
            finally:
                if self._current_request_id == request_id:
                    self._current_request_id = None
                if self._current_task is task:
                    self._current_task = None

    def cancel(self) -> bool:
        """中断进行中请求. 无活动请求返回 False."""
        if self._current_request_id is None:
            return False
        self.chat.cancel()
        return True

    def status(self) -> dict:
        return self.chat.stats.to_dict()


# ── HTTP 请求/响应模型 ──────────────────────────────────


class ChatRequest(BaseModel):
    prompt: str


# ── App 工厂 ───────────────────────────────────────────


def create_app(
    project_dir: str = ".",
    model: str | None = None,
    *,
    session: ChatSession | None = None,
) -> FastAPI:
    """构建 FastAPI app.

    Args:
        project_dir: ChatSession 工作目录(复用 chat 命令构造默认).
        model: 模型名(复用 ChatSession 默认).
        session: 注入已构造的 ChatSession(测试用); 默认按 chat 命令方式新建.
    """
    chat = session if session is not None else ChatSession(project_dir=project_dir, model=model)
    state = ServerSession(chat)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.chat.start()
        try:
            yield
        finally:
            await state.chat.close()

    app = FastAPI(lifespan=lifespan, title="autoloop-agent server")

    @app.post("/chat")
    async def chat_endpoint(req: ChatRequest) -> dict:
        """触发 session.send(), 立即返回 ack 不阻塞."""
        request_id = state.submit(req.prompt)
        return {"status": "accepted", "request_id": request_id}

    @app.get("/events")
    async def events_endpoint() -> EventSourceResponse:
        """SSE: 从 asyncio.Queue 实时推送序列化 ChatEvent."""
        q = state.subscribe()

        async def event_stream() -> AsyncIterator[dict]:
            try:
                # 首条注释: 触发 SSE 头部 flush(sse_starlette 首条前不发 response headers,
                # 若直接 await q.get() 会让客户端 stream 上下文挂起). 注释被 EventSource 忽略.
                yield {"comment": "connected"}
                while True:
                    event = await q.get()
                    yield {"data": json.dumps(event.to_dict(), ensure_ascii=False)}
            finally:
                state.unsubscribe(q)

        return EventSourceResponse(event_stream())

    @app.post("/cancel")
    async def cancel_endpoint() -> dict:
        ok = state.cancel()
        return {"status": "cancelling" if ok else "idle"}

    @app.get("/status")
    async def status_endpoint() -> dict:
        return state.status()

    return app
