"""共享上下文提供者 — 可插拔接口

⚠️ 方案设计：
   上下文和缓存的具体实现方案由用户提供。
   本模块只定义 ChatSession 需要的接口协议。
   用户实现 ContextProvider 后注入 ChatSession 即可。

接口职责：
   1. build_system_prompt()   — 将上下文组装为 system_prompt 注入 SDK
   2. on_turn_end()           — Agent 完成一轮对话后更新上下文
   3. has_context_changed()   — 检测上下文是否发生关键变更（触发 Client 热重启）
   4. get_history_summary()   — 获取历史摘要（Client 重启时灌入防"失忆"）
   5. get_context_summary()   — 给 /context show 命令返回摘要

关于动态上下文热更新：
   ─────────────────────────────────────────
   问题：ClaudeSDKClient 启动后，system_prompt 是固定的。
   但在多轮对话中上下文可能变化（新 .skills 沉淀、项目切换等），
   存活的 Client 实例无法动态热更新 System Prompt。

   解决方案：
   每轮对话结束后，ChatSession 调用 has_context_changed()。
   如果返回 True，ChatSession 平滑重启底层 Client：
     1. 调用 get_history_summary() 获取对话历史摘要
     2. 关闭旧 Client
     3. 用新的 system_prompt（含更新后的上下文）创建新 Client
     4. 将历史摘要作为首条消息灌入新 Client
   对用户完全透明，不中断聊天体验。
   ─────────────────────────────────────────

关于 Claude Code 系统提示词：
   ─────────────────────────────────────────
   默认使用 Claude Code CLI 的完整系统提示词预设：
   {"type": "preset", "preset": "claude_code"}

   这使 Agent 获得与 Claude Code CLI 完全一致的能力。
   ─────────────────────────────────────────
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable, Union

# system_prompt 支持的类型：
# - str: 自定义系统提示词文本
# - dict: Claude Code preset 配置 {"type": "preset", "preset": "claude_code", ...}
SystemPromptType = Union[str, dict]


@runtime_checkable
class ContextProvider(Protocol):
    """共享上下文提供者协议

    任何实现了这些方法的类都可以作为 ChatSession 的上下文源。
    使用 Protocol 而非 ABC，允许鸭子类型。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> SystemPromptType:
        """返回 system_prompt 配置

        Args:
            base_prompt:  BaseAgent 原始 system_prompt（使用 preset 时忽略）
            project_dir:  当前项目目录

        Returns:
            system_prompt 配置，支持两种格式：

            1. Claude Code preset（推荐）：
               {"type": "preset", "preset": "claude_code"}

            2. 自定义字符串：
               "你是一个开发助手..."
        """
        ...

    def on_turn_end(
        self,
        prompt: str,
        response_summary: str,
        tool_calls: list[dict],
    ) -> None:
        """一轮对话结束后的回调 — 用于更新上下文

        Args:
            prompt:           用户本轮输入
            response_summary: Agent 回复摘要
            tool_calls:       本轮工具调用记录
        """
        ...

    def has_context_changed(self) -> bool:
        """检测上下文是否发生了关键变更

        ChatSession 在每轮对话结束后调用此方法。
        如果返回 True，ChatSession 将平滑重启底层 Client
        以应用最新的上下文。
        """
        ...

    def get_history_summary(
        self,
        collected_texts: list[str],
        tool_calls: list[dict],
        turn_count: int,
    ) -> str:
        """生成对话历史摘要 — 用于 Client 重启时防失忆"""
        ...

    def get_context_summary(self) -> str:
        """返回当前上下文的摘要 — 供 /context show 命令使用"""
        ...


class DefaultContextProvider:
    """默认上下文提供者 — 使用 Claude Code CLI 完整系统提示词

    返回 Claude Code preset，获得与 Claude Code CLI 完全一致的能力。
    不追加任何自定义内容。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> SystemPromptType:
        """返回 Claude Code CLI 的完整系统提示词"""
        return {"type": "preset", "preset": "claude_code"}

    def on_turn_end(
        self,
        prompt: str,
        response_summary: str,
        tool_calls: list[dict],
    ) -> None:
        pass

    def has_context_changed(self) -> bool:
        return False

    def get_history_summary(
        self,
        collected_texts: list[str],
        tool_calls: list[dict],
        turn_count: int,
    ) -> str:
        return ""

    def get_context_summary(self) -> str:
        return "[使用 Claude Code 系统提示词]"
