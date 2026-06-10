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
- ❌ 理用户输入（交给 REPL）
- ❌ 设计上下文方案（交给 ContextProvider）
"""

from __future__ import annotations

import asyncio
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
    cancelled_event,
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
        self.cancelled_requests: int = 0  # 取消请求计数

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_tool_calls": self.total_tool_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cancelled_requests": self.cancelled_requests,
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
        self.model = model  # 用户指定的模型（可能为 None，表示使用全局配置）
        self.max_turns = max_turns
        self.context_provider = context_provider or DefaultContextProvider()
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]

        self._client: ClaudeSDKClient | None = None
        self.stats = SessionStats()
        self._is_active = False
        self._all_collected_texts: list[str] = []
        self._all_tool_calls: list[dict] = []

        # 实际使用的模型名称（从 SDK 返回中提取）
        self.actual_model: str | None = None

        # 取消标志
        self._cancelled: bool = False
        # 当前正在处理请求的标志
        self._processing_request: bool = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def is_cancelled(self) -> bool:
        """检查是否已被取消"""
        return self._cancelled

    @property
    def is_processing(self) -> bool:
        """检查是否正在处理请求"""
        return self._processing_request

    def cancel(self) -> None:
        """取消当前请求

        设置取消标志，并在异步上下文中中断 SDK 请求。
        """
        self._cancelled = True
        # 异步中断会在 send() 方法中处理

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
        # 如果正在处理请求，先中断
        if self._processing_request and self._client:
            try:
                await self._client.interrupt()
            except Exception:
                pass

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

    async def _recover_client_after_cancel(self) -> None:
        """取消后恢复客户端状态

        中断请求后，SDK 客户端可能处于不稳定状态，
        需要断开重连以恢复。
        """
        if not self._client:
            return

        try:
            # 先断开
            await self._client.disconnect()
            # 重新连接
            await self._client.connect()
        except Exception:
            # 如果重连失败，尝试完全重建客户端
            try:
                await self._client.__aexit__(None, None, None)
                options = self._build_options()
                self._client = ClaudeSDKClient(options=options)
                await self._client.__aenter__()
            except Exception:
                pass  # 静默处理，下次请求时会检查状态

    async def send(self, prompt: str) -> AsyncIterator[ChatEvent]:
        """发送一条消息，返回事件流

        这是 ChatSession 的核心方法。
        将用户输入发送给 SDK，将 SDK 返回的 Message
        逐一转换为 ChatEvent 并 yield。

        支持取消：当用户按下 ESC 时，会设置 _cancelled 标志，
        本方法会中断 SDK 请求并恢复客户端状态。

        Args:
            prompt: 用户输入的文本

        Yields:
            ChatEvent: 供 Renderer 消费的事件流
        """
        if not self._client or not self._is_active:
            yield error_event("会话未启动，请先调用 start()")
            return

        # 清除取消标志，开始新请求
        self._cancelled = False
        self._processing_request = True

        self.stats.turn_count += 1
        turn_number = self.stats.turn_count
        turn_tool_count = 0
        collected_texts: list[str] = []
        tool_calls: list[dict] = []
        start_time = time.monotonic()

        # ── TURN_START ──
        yield turn_start_event(turn_number, prompt)

        was_cancelled = False

        try:
            await self._client.query(prompt)

            async for message in self._client.receive_response():
                # 检查取消标志 — 立即中断
                if self._cancelled:
                    was_cancelled = True
                    self.stats.cancelled_requests += 1
                    # 真正中断 SDK 的响应流
                    try:
                        await self._client.interrupt()
                    except Exception:
                        pass
                    yield cancelled_event("用户取消了请求")
                    break

                # ── AssistantMessage ──
                if isinstance(message, AssistantMessage):
                    # 从 AssistantMessage 中提取实际使用的模型名称
                    if hasattr(message, "model") and message.model:
                        self.actual_model = message.model

                    for block in message.content:
                        # 取消时不再处理后续 block
                        if self._cancelled:
                            break

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
                        # 尝试从 model_usage 中提取模型名称（这是最准确的来源）
                        # model_usage 是一个 dict: {'model_name': {...usage info...}}
                        model_usage = getattr(message, "model_usage", None)
                        if model_usage and isinstance(model_usage, dict):
                            # 取第一个模型名（通常只有一个）
                            model_names = list(model_usage.keys())
                            if model_names:
                                self.actual_model = model_names[0]

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

                            self.stats.total_input_tokens += input_t
                            self.stats.total_output_tokens += output_t

                            yield usage_event(
                                input_tokens=input_t,
                                output_tokens=output_t,
                                cache_read_tokens=cache_r,
                                cache_creation_tokens=cache_c,
                                model_name=self.actual_model,
                            )

        except asyncio.CancelledError:
            # 异步任务被取消
            was_cancelled = True
            self.stats.cancelled_requests += 1
            try:
                await self._client.interrupt()
            except Exception:
                pass
            yield cancelled_event("用户取消了请求")

        except Exception as e:
            yield error_event(f"SDK 异常: {e}")
            collected_texts.append(f"[错误] {e}")

        finally:
            self._processing_request = False

            # 如果被取消，恢复客户端状态
            if was_cancelled:
                await self._recover_client_after_cancel()

        # ── 更新统计 + 历史记录（取消时跳过） ──
        if not was_cancelled:
            self.stats.total_tool_calls += turn_tool_count
            self._all_collected_texts.extend(collected_texts)
            self._all_tool_calls.extend(tool_calls)

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

        duration_ms = int((time.monotonic() - start_time) * 1000)

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