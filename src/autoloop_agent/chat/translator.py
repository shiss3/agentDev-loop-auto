"""MessageTranslator — SDK Message/Block → ChatEvent 转换

把 claude_agent_sdk 的消息（AssistantMessage / ResultMessage / RateLimitEvent /
StreamEvent）逐条转换为 ChatEvent。这一层是**无状态**的纯转换逻辑：

  translate(message) -> TranslationResult

TranslationResult 同时携带：
  - events:   要 yield 给 Renderer 的 ChatEvent 列表
  - 侧数据：   调用方据此更新自己的统计/上下文（model 名、token 数、
              文本、工具调用等），转换器本身不持有会话状态。

⚠️ 设计动机：
  Phase 2 的 L2 子 Agent 节点也要把同样的 SDK 消息转成 ChatEvent，
  抽成独立无状态单元后两边复用、且可脱离 SDK 单测。

绝不做的事：
- ❌ 持有 ClaudeSDKClient / 会话状态
- ❌ 直接修改 SessionStats（由调用方根据 result 更新）
- ❌ print() / 终端输出
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    RateLimitEvent,
    StreamEvent,
)

from autoloop_agent.chat.events import (
    ChatEvent,
    text_event,
    tool_use_event,
    error_event,
    usage_event,
    rate_limit_event,
    stream_error_event,
)


@dataclass
class TranslationResult:
    """单条消息的转换结果：事件 + 供调用方更新状态的侧数据"""

    events: list[ChatEvent] = field(default_factory=list)
    # 本条消息收集到的文本块（用于 ContextProvider 摘要）
    texts: list[str] = field(default_factory=list)
    # 本条消息的工具调用信息（{tool_name, tool_id, tool_input}）
    tool_calls: list[dict] = field(default_factory=list)
    tool_count: int = 0
    # 若消息携带模型名则非 None（调用方据此更新 actual_model）
    model_name: str | None = None
    # token 用量（仅 ResultMessage 携带 usage 时非零）
    input_tokens: int = 0
    output_tokens: int = 0
    # 速率限制状态（仅 RateLimitEvent 时非 None）
    rate_limit_status: str | None = None
    rate_limit_rejected: bool = False
    # 成本（美元，仅 ResultMessage.total_cost_usd 携带时非零）
    total_cost_usd: float = 0.0


def summarize_tool_input(tool_input: dict) -> str:
    """工具输入摘要化（复用 Phase 1 逻辑）"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:100]
    if "command" in tool_input:
        return tool_input["command"][:100]
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "content" in tool_input:
        return f"[{len(tool_input['content'])} chars]"
    return str(tool_input)[:100]


class MessageTranslator:
    """无状态的 SDK 消息 → ChatEvent 转换器"""

    def translate(self, message: object, model_name: str = "default") -> TranslationResult:
        """转换单条 SDK 消息

        Args:
            message: SDK 产出的消息对象
            model_name: 当前已知的模型名（用于 usage_event 的展示）

        Returns:
            TranslationResult: 事件列表 + 状态侧数据
        """
        if isinstance(message, RateLimitEvent):
            return self._translate_rate_limit(message)
        if isinstance(message, StreamEvent):
            return self._translate_stream(message)
        if isinstance(message, AssistantMessage):
            return self._translate_assistant(message)
        if isinstance(message, ResultMessage):
            return self._translate_result(message, model_name)
        return TranslationResult()

    # ── 各消息类型转换 ──

    def _translate_rate_limit(self, message: RateLimitEvent) -> TranslationResult:
        rli = message.rate_limit_info
        status = rli.status
        result = TranslationResult(rate_limit_status=status)

        if status == "rejected":
            result.rate_limit_rejected = True

        # 构建人类可读消息
        msg = ""
        if status == "rejected":
            msg = "⚠️ API 请求被拒绝（速率限制）"
            if rli.resets_at:
                reset_time = datetime.datetime.fromtimestamp(rli.resets_at)
                msg += f"，将在 {reset_time.strftime('%H:%M:%S')} 重置"
        elif status == "allowed_warning":
            util = rli.utilization
            msg = f"⚠️ API 速率接近限制（已使用 {int((util or 0) * 100)}%）"
        elif rli.rate_limit_type:
            msg = f"ℹ️ 速率限制类型: {rli.rate_limit_type}"

        result.events.append(rate_limit_event(
            status=status,
            rate_limit_type=rli.rate_limit_type,
            resets_at=rli.resets_at,
            utilization=rli.utilization,
            message=msg,
        ))
        return result

    def _translate_stream(self, message: StreamEvent) -> TranslationResult:
        result = TranslationResult()
        event_data = message.event or {}
        event_type = event_data.get("type", "") if isinstance(event_data, dict) else ""

        # 检测 stream 错误（如 "error" 类型的 event）
        if isinstance(event_data, dict):
            if event_data.get("type") == "error" or event_data.get("error"):
                err_msg = event_data.get("error", str(event_data))
                result.events.append(stream_error_event(
                    error=str(err_msg)[:300],
                    stream_event_type=event_type,
                ))
        return result

    def _translate_assistant(self, message: AssistantMessage) -> TranslationResult:
        result = TranslationResult()

        # 从 AssistantMessage 中提取实际使用的模型名称
        if getattr(message, "model", None):
            result.model_name = message.model

        for block in message.content:
            if isinstance(block, TextBlock):
                result.texts.append(block.text)
                result.events.append(text_event(block.text))

            elif isinstance(block, ToolUseBlock):
                result.tool_count += 1
                input_summary = summarize_tool_input(block.input)
                result.tool_calls.append({
                    "tool_name": block.name,
                    "tool_id": block.id,
                    "tool_input": input_summary,
                })
                result.events.append(tool_use_event(
                    tool_name=block.name,
                    tool_id=block.id,
                    tool_input=input_summary,
                ))

        return result

    def _translate_result(self, message: ResultMessage, model_name: str) -> TranslationResult:
        result = TranslationResult()

        # 成本提取（ResultMessage.total_cost_usd）
        result.total_cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)

        if getattr(message, "is_error", False):
            errors = getattr(message, "errors", [])
            err_text = "\n".join(errors) if errors else "未知错误"
            result.events.append(error_event(err_text[:500]))
            return result

        # 尝试从 model_usage 中提取模型名称（这是最准确的来源）
        # model_usage 是一个 dict: {'model_name': {...usage info...}}
        model_usage = getattr(message, "model_usage", None)
        if model_usage and isinstance(model_usage, dict):
            model_names = list(model_usage.keys())
            if model_names:
                result.model_name = model_names[0]
                model_name = model_names[0]

        # 尝试提取 usage 信息
        usage = getattr(message, "usage", None)
        if usage:
            # usage 可能是 dict 或对象
            if isinstance(usage, dict):
                input_t = usage.get("input_tokens", 0) or 0
                output_t = usage.get("output_tokens", 0) or 0
                cache_r = usage.get("cache_read_input_tokens", 0) or 0
                cache_c = usage.get("cache_creation_input_tokens", 0) or 0
            else:
                input_t = getattr(usage, "input_tokens", 0) or 0
                output_t = getattr(usage, "output_tokens", 0) or 0
                cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
                cache_c = getattr(usage, "cache_creation_input_tokens", 0) or 0

            result.input_tokens = input_t
            result.output_tokens = output_t

            result.events.append(usage_event(
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read_tokens=cache_r,
                cache_creation_tokens=cache_c,
                model_name=model_name,
            ))

        return result
