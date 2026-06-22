"""ChatSession — 多轮对话会话管理

核心职责：
1. 包装 ClaudeSDKClient，管理会话生命周期
2. 将 SDK 的 Message/Block 转换为 ChatEvent 事件流
3. 注入共享上下文到 system_prompt
4. 维护对话统计（轮次 / Token / 工具调用数）
5. 检测上下文变更，平滑重启底层 Client

⚠️ 系统提示词配置：
   通过 ContextProvider.build_system_prompt() 返回的值决定使用的系统提示词：
   - 字符串: 自定义系统提示词
   - {"type": "preset", "preset": "claude_code"}: 使用 Claude Code CLI 完整提示词

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
import datetime
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import AsyncIterator

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    SessionStore,
    RateLimitEvent,
    StreamEvent,
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
    retry_event,
    rate_limit_event,
    stream_error_event,
)
from harness_agent.chat.translator import MessageTranslator
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。"""

_logger = logging.getLogger(__name__)


class SessionStats:
    """会话统计"""

    def __init__(self) -> None:
        self.turn_count: int = 0
        self.total_tool_calls: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.cancelled_requests: int = 0  # 取消请求计数
        # ── 连接/重试监控 ──
        self.retry_count: int = 0          # 本会话累计重试次数
        self.rate_limit_hits: int = 0     # 进入 "rejected" 状态的次数
        self.last_retry_reason: str = ""  # 最近一次重试原因
        self.last_rate_limit_status: str = ""  # 最近一次 rate limit 状态

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_tool_calls": self.total_tool_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cancelled_requests": self.cancelled_requests,
            "retry_count": self.retry_count,
            "rate_limit_hits": self.rate_limit_hits,
            "last_retry_reason": self.last_retry_reason,
            "last_rate_limit_status": self.last_rate_limit_status,
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

    ⚠️ session_id 说明：
       SDK 的 session_id 只在客户端初始化时设置一次。
       多轮对话时，SDK 会自动维护会话状态，不需要重复设置 session_id。
       如果在重建客户端时重复使用相同的 session_id，会导致 "Session ID already in use" 错误。
    """

    def __init__(
        self,
        project_dir: str = ".",
        model: str | None = None,
        max_turns: int | None = None,
        context_provider: ContextProvider | None = None,
        allowed_tools: list[str] | None = None,
        session_store: SessionStore | None = None,
        resume_session_id: str | None = None,
        continue_conversation: bool = False,
        enable_undo: bool = False,  # 开启文件检查点模式（禁用会话恢复）
    ) -> None:
        self.project_dir = project_dir
        self.model = model  # 用户指定的模型（可能为 None，表示使用全局配置）
        self.max_turns = max_turns
        self.context_provider = context_provider or DefaultContextProvider()
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]

        # 会话模式选择
        # - enable_undo=True: 开启检查点模式（支持 /undo，但禁用会话恢复）
        # - enable_undo=False (默认): 会话恢复模式（支持 /continue，但无检查点）
        self.enable_undo = enable_undo
        self.session_store = None if enable_undo else session_store
        self.resume_session_id = None if enable_undo else resume_session_id
        self.continue_conversation = False if enable_undo else continue_conversation

        self._client: ClaudeSDKClient | None = None
        self.stats = SessionStats()
        self._is_active = False
        self._all_collected_texts: list[str] = []
        self._all_tool_calls: list[dict] = []

        # 会话 ID（用于恢复和持久化）
        # 注意：session_id 只在 start() 时使用一次，后续重建客户端时需要生成新的 session_id
        self.session_id: str = resume_session_id or str(uuid.uuid4()) if not enable_undo else str(uuid.uuid4())
        # 标记是否已经初始化过客户端（用于判断是否需要设置 session_id）
        self._client_initialized: bool = False

        # 实际使用的模型名称（从 SDK 返回中提取）
        self.actual_model: str | None = None

        # 无状态消息转换器（SDK Message → ChatEvent）
        self._translator = MessageTranslator()

        # 取消标志
        self._cancelled: bool = False
        # 当前正在处理请求的标志
        self._processing_request: bool = False

        # ── 连接/重试监控 ──
        self._retry_count: int = 0          # 当前请求的重试计数
        self._current_retry_reason: str = ""  # 当前重试原因
        self._retry_lock = threading.Lock()  # 保护 retry 计数的线程锁
        # stderr 回调（由 _handle_stderr_line 设置，SDK 会调用它）
        self._stderr_callback: Callable[[str], None] | None = None

    # ── CLI retry 日志检测模式（类属性）──
    # Anthropic CLI 会在连接失败时打印类似 "Retrying... (attempt 2/5)" 的日志
    _RETRY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        # "Retrying... (attempt 2/5)" 或 "Retrying request (2/5)"
        (re.compile(r"retrying.*?\((\d+)/(\d+)\)", re.I), "retry_attempt"),
        # "Retrying in 5 seconds..."
        (re.compile(r"retrying in (\d+) second", re.I), "retry_wait"),
        # "Connection failed, retrying..."
        (re.compile(r"connection failed", re.I), "connection_failed"),
        # "Request failed with status 429, retrying..."
        (re.compile(r"(?:status |error )?429.*retry", re.I), "rate_limit"),
        # "Request timeout, retrying..."
        (re.compile(r"timeout.*retry", re.I), "timeout"),
        # "Error.*retrying" (通用重试)
        (re.compile(r"error.*retrying", re.I), "error"),
    ]

    def _handle_stderr_line(self, line: str) -> None:
        """处理 CLI stderr 行，检测 retry 信息

        当 SDK 从 Claude Code CLI 子进程的 stderr 读取到内容时调用。
        我们从这些行中解析重试状态并记录到统计中。

        注意：这是同步回调，在 SDK 的 I/O 线程中执行，不能 yield 事件。
        事件会在 send() 循环中通过检测 _retry_count 来处理。
        """
        for pattern, reason in self._RETRY_PATTERNS:
            match = pattern.search(line)
            if match:
                # 提取 attempt 数（如果提供）
                attempt: int = 1
                max_attempts: int | None = None
                if len(match.groups()) >= 1 and match.group(1):
                    g1 = match.group(1)
                    if g1 and g1.isdigit():
                        attempt = int(g1)
                if len(match.groups()) >= 2 and match.group(2):
                    g2 = match.group(2)
                    if g2 and g2.isdigit():
                        max_attempts = int(g2)

                # 使用线程锁保护共享状态
                with self._retry_lock:
                    self._retry_count = attempt
                    self._current_retry_reason = reason
                    # 更新会话统计（取最大值）
                    if attempt > self.stats.retry_count:
                        self.stats.retry_count = attempt
                    self.stats.last_retry_reason = reason

                _logger.debug(
                    "Retry detected: attempt=%s, reason=%s, max=%s",
                    attempt,
                    reason,
                    max_attempts,
                )
                break

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

    def _build_options(self, for_rebuild: bool = False) -> ClaudeAgentOptions:
        """构建 SDK 配置，注入共享上下文

        Args:
            for_rebuild: 是否用于重建客户端（取消后恢复或上下文变更热重启）
                        如果为 True，不设置 session_id，让 SDK 自动管理
                        如果为 False，按正常逻辑设置 session_id

        注意：当 system_prompt 使用 claude_code preset 时，
        SDK 会调用 Claude Code CLI，自动获得 Claude Code 的所有默认行为：
        - 默认工具集 (Read, Write, Edit, Bash 等)
        - 默认 skills (/help, /review 等)
        - 默认 hooks (如果有配置)
        - 默认权限模式

        注意：session_store 和 enable_file_checkpointing 不能同时使用，
        因为检查点是本地存储，与远程会话记录会冲突。

        ⚠️ session_id 重复使用问题修复：
           SDK 的 session_id 只在客户端初始化时生效。
           如果在重建客户端时使用相同的 session_id，SDK 子进程可能检测到
           "Session ID already in use" 错误。

           解决方案：
           - 第一次初始化客户端时设置 session_id
           - 重建客户端时不设置 session_id，让 SDK 自动管理会话延续
        """
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

            # ── Claude Code 增强配置 ──
            include_partial_messages=True,  # 流式输出时包含部分消息
            stderr=self._handle_stderr_line,  # 捕获 CLI stderr（包含 retry 日志）
        )

        # 注意：session_store 和 enable_file_checkpointing 不能同时使用
        # 如果启用了 enable_undo，跳过 session_store
        if self.enable_undo:
            # 检查点模式：开启文件检查点，禁用会话恢复
            opts.enable_file_checkpointing = True
        else:
            # 会话恢复模式：使用 session_store
            if self.session_store:
                opts.session_store = self.session_store

        if self.model:
            opts.model = self.model

        # ── session_id 设置逻辑（修复重复使用问题）──
        # 重建客户端时不设置 session_id，避免 "Session ID already in use" 错误
        if for_rebuild:
            # 重建时让 SDK 自动管理会话延续，不设置 session_id
            # 这样可以避免旧的 SDK 子进程检测到 session_id 冲突
            pass  # 不设置任何 session 相关参数
        elif self.enable_undo:
            # 检查点模式：只在第一轮设置 session_id
            if not self._client_initialized:
                opts.session_id = self.session_id
        elif self.resume_session_id:
            # 恢复指定会话
            opts.resume = self.resume_session_id
        elif self.continue_conversation:
            # 恢复最近会话
            opts.continue_conversation = True
        else:
            # 新会话模式：只在第一轮设置 session_id
            if not self._client_initialized:
                opts.session_id = self.session_id

        return opts

    async def start(self) -> None:
        """启动会话 — 初始化 ClaudeSDKClient"""
        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        self._is_active = True
        self._client_initialized = True  # 标记客户端已初始化

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
        self._client_initialized = False  # 重置初始化标志

    async def _hot_restart_client(self) -> None:
        """平滑重启底层 Client — 应用最新上下文

        ⚠️ 修复：使用 for_rebuild=True 参数，避免 session_id 重复使用错误。
        """
        history_summary = self.context_provider.get_history_summary(
            collected_texts=self._all_collected_texts,
            tool_calls=self._all_tool_calls,
            turn_count=self.stats.turn_count,
        )

        if self._client:
            await self._client.__aexit__(None, None, None)

        # 使用 for_rebuild=True，不设置 session_id，让 SDK 自动管理会话延续
        options = self._build_options(for_rebuild=True)
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

        ⚠️ 修复：使用 for_rebuild=True 参数，避免 session_id 重复使用错误。
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
                # 使用 for_rebuild=True，不设置 session_id
                options = self._build_options(for_rebuild=True)
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

        # ── 重试监控：每个请求开始时重置计数器（加锁，stderr 线程会并发写）──
        with self._retry_lock:
            self._retry_count = 0
            self._current_retry_reason = ""
        last_emitted_retry_count = 0  # 用于检测 retry 计数变化

        try:
            await self._client.query(prompt)

            async for message in self._client.receive_response():
                # ── 检查 retry 计数变化（由 stderr 回调跨线程更新，需加锁读取）──
                with self._retry_lock:
                    retry_count = self._retry_count
                    retry_reason = self._current_retry_reason
                if retry_count > last_emitted_retry_count:
                    yield retry_event(
                        attempt=retry_count,
                        reason=retry_reason,
                    )
                    last_emitted_retry_count = retry_count

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

                # ── 转换 SDK 消息为 ChatEvent（无状态转换器）──
                result = self._translator.translate(message, self.actual_model)

                # 更新模型名（AssistantMessage / ResultMessage 携带）
                if result.model_name:
                    self.actual_model = result.model_name

                # 速率限制统计
                if result.rate_limit_status is not None:
                    self.stats.last_rate_limit_status = result.rate_limit_status
                    if result.rate_limit_rejected:
                        self.stats.rate_limit_hits += 1

                # token 统计
                self.stats.total_input_tokens += result.input_tokens
                self.stats.total_output_tokens += result.output_tokens

                # 累计文本 / 工具调用（用于 turn 结束后的统计与上下文摘要）
                turn_tool_count += result.tool_count
                collected_texts.extend(result.texts)
                tool_calls.extend(result.tool_calls)

                # 逐个 yield 事件（取消时停止后续事件）
                for ev in result.events:
                    if self._cancelled:
                        break
                    yield ev

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