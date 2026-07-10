"""Governor 共用消息构造工具。"""

from harness_agent.chat.events import ChatEvent, text_event


def _build_message(text: str, agent: str = "default") -> ChatEvent:
    """构造 Governor 系统消息（调度提示、降级提示、交付轨占位等）。

    薄封装 text_event —— 统一 Governor 消息构造入口，未来可扩展（带 agent 名/metadata）。
    """
    return text_event(text, agent=agent)
