"""autoloop serve - FastAPI + SSE 适配层.

复用 ChatSession.send() 的 ChatEvent 事件流作为协议层:
  POST /chat    -> 触发 session.send(), 立即返回 ack(不阻塞)
  GET  /events  -> SSE 实时推送序列化 ChatEvent(asyncio.Queue 订阅广播)
  POST /cancel  -> 中断进行中请求(session.cancel)
  GET  /status  -> SessionStats(轮次/token/成本)

单会话模型: 一个 ChatSession + asyncio.Lock 串行化并发 send,
防同会话状态撞车. serve 与 chat 进程级隔离, 不共享 session 状态.
不改 BaseAgentSession.send() 签名, 仅消费其 AsyncIterator[ChatEvent].
"""

from autoloop_agent.server.app import ServerSession, create_app

__all__ = ["ServerSession", "create_app"]
