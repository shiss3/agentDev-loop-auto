"""ChatSession — 多轮对话会话管理

核心职责：
1. 包装 ClaudeSDKClient，管理会话生命周期
2. 将 SDK 的 Message/Block 转换为 ChatEvent 事件流
3. 注入共享上下文到 system_prompt
4. 维护对话统计（轮次 / Token / 工具调用数）
5. 检测上下文变更，平滑重启底层 Client

⚠️ 过渡态说明：
   ChatSession 是 Phase 2 的单 Agent 封装，Phase 3 中
   多轮对话管理权将交还给 LangGraph StateGraph + Checkpointer。
   此时 ChatSession 退役，但以下部分会被复用：
   - ChatEvent 事件协议 → LangGraph 节点也产出 ChatEvent
   - ChatRenderer → 继续消费 ChatEvent
   - ContextProvider → 继续通过 system_prompt 注入上下文

绝不做的事：
- ❌ print() / 终端输出（交给 Renderer）
- ❌ 管理用户输入（交给 REPL）
- ❌ 设计上下文方案（交给 ContextProvider）
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from harness_agent.chat.events import (
    ChatEvent,
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    usage_event,
)
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。"""


class SessionStats:
    """会话统计"""

    def __init__(self) -> None:
        self.turn_count: int = 0
        self.total_tool_calls: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_tool_calls": self.total_tool_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }


class ChatSession:
    """多轮对话会话

    使用方式：

        session = ChatSession(project_dir="./my-app")
        await session.start()

        # 多轮对话
        async for event in session.send("重构 user.py"):
            renderer.handle(event)

        async for event in session.send("再加个单元测试"):
            renderer.handle(event)

        await session.close()
    """

    def __init__(
        self,
        project_dir: str = ".",
        model: str | None = None,
        max_turns: int = 30,
        context_provider: ContextProvider | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.model = model
        self.max_turns = max_turns
        self.context_provider = context_provider or DefaultContextProvider()
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]

        self._client: ClaudeSDKClient | None = None
        self.stats = SessionStats()
        self._is_active = False
        self._all_collected_texts: list[str] = []
        self._all_tool_calls: list[dict] = []

    @property
    def is_active(self) -> bool:
        return self._is_active

    def _build_options(self) -> ClaudeAgentOptions:
        """构建 SDK 配置，注入共享上下文"""
        # 通过 ContextProvider 组装 system_prompt
        system_prompt = self.context_provider.build_system_prompt(
            base_prompt=_BASE_SYSTEM_PROMPT,
            project_dir=self.project_dir,
        )

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=self.project_dir,
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            permission_mode="acceptEdits",
        )

        if self.model:
            opts.model = self.model

        return opts

    async def start(self) -> None:
        """启动会话 — 初始化 ClaudeSDKClient"""
        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        self._is_active = True

    async def close(self) -> None:
        """关闭会话 — 清理资源"""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
        self._is_active = False

    async def _hot_restart_client(self) -> None:
        """平滑重启底层 Client — 应用最新上下文"""
        history_summary = self.context_provider.get_history_summary(
            collected_texts=self._all_collected_texts,
            tool_calls=self._all_tool_calls,
            turn_count=self.stats.turn_count,
        )

        if self._client:
            await self._client.__aexit__(None, None, None)

        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()

        if history_summary:
            await self._client.query(
                f"[系统] 以下是之前对话的摘要，请基于此继续：\n{history_summary}"
            )
            async for _ in self._client.receive_response():
                pass

    async def send(self, prompt: str) -> AsyncIterator[ChatEvent]:
        """发送一条消息，返回事件流

        这是 ChatSession 的核心方法。
        将用户输入发送给 SDK，将 SDK 返回的 Message
        逐一转换为 ChatEvent 并 yield。

        Args:
            prompt: 用户输入的文本

        Yields:
            ChatEvent: 供 Renderer 消费的事件流
        """
        if not self._client or not self._is_active:
            yield error_event("会话未启动，请先调用 start()")
            return

        self.stats.turn_count += 1
        turn_number = self.stats.turn_count
        turn_tool_count = 0
        collected_texts: list[str] = []
        tool_calls: list[dict] = []
        start_time = time.monotonic()

        # ── TURN_START ──
        yield turn_start_event(turn_number, prompt)

        try:
            await self._client.query(prompt)

            async for message in self._client.receive_response():

                # ── AssistantMessage ──
                if isinstance(message, AssistantMessage):
                    for block in message.content:

                        if isinstance(block, TextBlock):
                            collected_texts.append(block.text)
                            yield text_event(block.text)

                        elif isinstance(block, ToolUseBlock):
                            turn_tool_count += 1
                            input_summary = _summarize_tool_input(block.input)
                            tool_info = {
                                "tool_name": block.name,
                                "tool_id": block.id,
                                "tool_input": input_summary,
                            }
                            tool_calls.append(tool_info)
                            yield tool_use_event(
                                tool_name=block.name,
                                tool_id=block.id,
                                tool_input=input_summary,
                            )

                # ── ResultMessage ──
                elif isinstance(message, ResultMessage):
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", [])
                        err_text = "\n".join(errors) if errors else "未知错误"
                        yield error_event(err_text[:500])
                    else:
                        # 尝试提取 usage 信息
                        usage = getattr(message, "usage", None)
                        if usage:
                            input_t = getattr(usage, "input_tokens", 0) or 0
                            output_t = getattr(usage, "output_tokens", 0) or 0
                            cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
                            cache_c = (
                                getattr(usage, "cache_creation_input_tokens", 0) or 0
                            )
                            self.stats.total_input_tokens += input_t
                            self.stats.total_output_tokens += output_t
                            yield usage_event(
                                input_tokens=input_t,
                                output_tokens=output_t,
                                cache_read_tokens=cache_r,
                                cache_creation_tokens=cache_c,
                            )

        except Exception as e:
            yield error_event(f"SDK 异常: {e}")
            collected_texts.append(f"[错误] {e}")

        # ── 更新统计 + 历史记录 ──
        self.stats.total_tool_calls += turn_tool_count
        self._all_collected_texts.extend(collected_texts)
        self._all_tool_calls.extend(tool_calls)
        duration_ms = int((time.monotonic() - start_time) * 1000)

        # ── 通知 ContextProvider 更新上下文 ──
        response_summary = "\n".join(collected_texts)[:2000]
        self.context_provider.on_turn_end(
            prompt=prompt,
            response_summary=response_summary,
            tool_calls=tool_calls,
        )

        # ── 检测上下文变更，触发 Client 热重启 ──
        if self.context_provider.has_context_changed():
            await self._hot_restart_client()

        # ── TURN_END ──
        yield turn_end_event(
            turn_number=turn_number,
            tool_count=turn_tool_count,
            duration_ms=duration_ms,
        )


def _summarize_tool_input(tool_input: dict) -> str:
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
