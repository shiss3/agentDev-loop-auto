"""L0Router 共用消息构造工具。"""

from harness_agent.chat.events import ChatEvent, text_event


def _build_message(text: str, agent: str = "default") -> ChatEvent:
    """构造 L0Router 系统消息（路由提示、降级提示、Full 占位等）。

    薄封装 text_event —— 统一 L0Router 消息构造入口，未来可扩展。
    """
    return text_event(text, agent=agent)
