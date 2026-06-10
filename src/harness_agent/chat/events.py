"""Chat 事件类型定义 — Session 与 Renderer 之间的协议

⚠️ 长期资产：
   ChatEvent 协议是跨 Phase 的稳定接口。
   Phase 3 引入 LangGraph 多 Agent 后，事件源会从 ChatSession
   切换到 LangGraph 节点，但 ChatRenderer 消费协议保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """事件类型枚举"""

    # ── Agent 输出 ──
    TEXT = "text"                    # 模型文本输出（思考/回答）
    TOOL_USE = "tool_use"            # 工具调用开始
    TOOL_RESULT = "tool_result"      # 工具执行结果

    # ── 会话生命周期 ──
    TURN_START = "turn_start"        # 一轮对话开始（用户发送消息后）
    TURN_END = "turn_end"            # 一轮对话结束（Agent 完成回复）

    # ── 状态 ──
    THINKING = "thinking"            # Agent 正在思考（工具调用间隙）
    ERROR = "error"                  # 错误

    # ── 统计 ──
    USAGE = "usage"                  # Token 用量统计


@dataclass
class ChatEvent:
    """单个事件

    Session 层产出 → Renderer 层消费。
    所有终端渲染决策由 Renderer 根据 event.type 决定。

    Attributes:
        type:      事件类型
        data:      事件数据（结构由 type 决定）
        agent:     产出事件的 Agent 名称
        timestamp: 事件产生时间
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    agent: str = "default"
    timestamp: datetime = field(default_factory=datetime.now)


# ── 便捷工厂函数 ──────────────────────────────────────


def text_event(text: str, agent: str = "default") -> ChatEvent:
    return ChatEvent(type=EventType.TEXT, data={"text": text}, agent=agent)


def tool_use_event(
    tool_name: str,
    tool_id: str,
    tool_input: str,
    agent: str = "default",
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TOOL_USE,
        data={
            "tool_name": tool_name,
            "tool_id": tool_id,
            "tool_input": tool_input,
        },
        agent=agent,
    )


def tool_result_event(
    tool_use_id: str,
    content: str,
    is_error: bool = False,
    agent: str = "default",
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TOOL_RESULT,
        data={
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        },
        agent=agent,
    )


def turn_start_event(turn_number: int, prompt: str) -> ChatEvent:
    return ChatEvent(
        type=EventType.TURN_START,
        data={"turn_number": turn_number, "prompt": prompt},
    )


def turn_end_event(
    turn_number: int,
    tool_count: int,
    duration_ms: int,
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TURN_END,
        data={
            "turn_number": turn_number,
            "tool_count": tool_count,
            "duration_ms": duration_ms,
        },
    )


def error_event(error: str, agent: str = "default") -> ChatEvent:
    return ChatEvent(
        type=EventType.ERROR, data={"error": error}, agent=agent
    )


def usage_event(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    model_name: str | None = None,
) -> ChatEvent:
    """创建 usage 事件

    Args:
        input_tokens: 输入 token 数
        output_tokens: 输出 token 数
        cache_read_tokens: 缓存读取 token 数
        cache_creation_tokens: 缓存创建 token 数
        model_name: 实际使用的模型名称（从 SDK 返回中提取）
    """
    data: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
    }
    if model_name:
        data["model_name"] = model_name
    return ChatEvent(type=EventType.USAGE, data=data)


def thinking_event(agent: str = "default") -> ChatEvent:
    return ChatEvent(type=EventType.THINKING, data={}, agent=agent)
