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
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextProvider(Protocol):
    """共享上下文提供者协议

    任何实现了这些方法的类都可以作为 ChatSession 的上下文源。
    使用 Protocol 而非 ABC，允许鸭子类型。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> str:
        """将共享上下文注入 system_prompt

        Args:
            base_prompt:  BaseAgent 原始 system_prompt
            project_dir:  当前项目目录

        Returns:
            组装后的完整 system_prompt，包含：
            - 原始 base_prompt
            - 全局上下文（项目结构、技术栈等）
            - 领域上下文（前端/后端/接口）
            - 缓存的经验 (.skills)
            - 其他用户定义的上下文
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
    """默认上下文提供者 — 透传原始 prompt，不注入任何上下文

    在用户提供具体实现之前作为占位符。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> str:
        return base_prompt

    def on_turn_end(
        self,
        prompt: str,
        response_summary: str,
        tool_calls: list[dict],
    ) -> None:
        pass  # 默认不做任何事

    def has_context_changed(self) -> bool:
        return False  # 默认上下文永远不变

    def get_history_summary(
        self,
        collected_texts: list[str],
        tool_calls: list[dict],
        turn_count: int,
    ) -> str:
        return ""  # 默认无历史摘要

    def get_context_summary(self) -> str:
        return "[未配置共享上下文]"
