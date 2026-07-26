"""ChatCLI — REPL 聊天主循环

Phase 2.5 (TUI 版):
  使用 TuiApp 全屏 TUI 替代 PromptSession + Rich Console。
  用户输入通过 TuiApp 的 Buffer accept 回调获取。
  所有输出通过 ContentBuffer → prompt_toolkit 渲染。

职责：
1. 管理 TuiApp / ChatRenderer / Governor 的生命周期
2. 分发用户输入（消息 vs 斜杠命令）
3. 处理退出信号
4. 处理请求取消
5. 会话恢复：启动时显示历史会话选择界面
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from claude_agent_sdk import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from harness_agent.chat.content_buffer import ContentBuffer
from harness_agent.chat.session_store import FileSessionStore, create_session_store
from harness_agent.chat.session_selector import SessionSelector, NEW_SESSION
from harness_agent.chat.tui_app import TuiApp
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.core.architect import Governor

# 导入 commands 以注册所有内置斜杠命令（必须保留此 import）
from harness_agent.chat import commands as _commands  # noqa: F401
from harness_agent.chat.commands import get_command

logger = logging.getLogger(__name__)

# 屏蔽 claude_agent_sdk logger.error 写 stderr 污染全屏 TUI。
# ESC 取消 -> SDK interrupt -> claude CLI 子进程退出码1 -> ProcessError ->
# SDK query.py logger.error("Fatal error in message reader: ...") 经 logging
# lastResort 写 sys.stderr，文本糊到 TUI 屏幕；resize 触发重绘才被覆盖
# （即"调整窗口大小后报错消失"）。取消的预期副作用（真错误已由 send() 的
# error_event 渲染到 ContentBuffer），故吞掉。
_sdk_logger = logging.getLogger("claude_agent_sdk")
_sdk_logger.addHandler(logging.NullHandler())
_sdk_logger.propagate = False


class ChatCLI:
    """REPL 聊天主循环（TUI 版）

    使用方式（由 cli.py 的 chat 命令调用）：

        cli = ChatCLI(project_dir="./my-app")
        await cli.run()
    """

    def __init__(
        self,
        project_dir: str = ".",
        model: str | None = None,
        history_file: str | None = None,
        session_store: FileSessionStore | None = None,
        resume_session_id: str | None = None,
        continue_conversation: bool = False,
        enable_undo: bool = False,  # 开启检查点模式（禁用会话恢复）
    ) -> None:
        self.project_dir = str(Path(project_dir).resolve())
        self.model = model

        # 会话模式
        # - enable_undo=True: 检查点模式（支持 /undo，禁用会话恢复）
        # - enable_undo=False (默认): 会话恢复模式
        self.enable_undo = enable_undo

        # 检查点模式下不使用 session_store
        if enable_undo:
            self.session_store = None
        else:
            self.session_store = session_store or create_session_store()

        self.resume_session_id = resume_session_id
        self.continue_conversation = continue_conversation

        # ── TUI 组件 ──
        self.content_buffer = ContentBuffer()
        self.tui = TuiApp(
            content_buffer=self.content_buffer,
            model_name=model or "default",
            history_file=history_file,
        )
        self.renderer = ChatRenderer(tui=self.tui)
        self.governor = self._create_session()

        # 状态
        self._should_exit = False
        # 调度模式：auto=解析需求后判轨（默认）；semi=输入直通交互轨（=原 /int）
        self.mode: str = "auto"
        # 当前消息处理的任务引用（用于取消）
        self._message_task: asyncio.Task | None = None
        # ── AskUserQuestion 待回答状态 ──
        # 非 None 时用户输入路由给该 Future（answer 模式），而非发新消息。
        self._pending_answer: asyncio.Future | None = None
        self._pending_options: list[str] = []  # 当前问题选项（数字映射用）

    def _create_session(self) -> Governor:
        return Governor(
            project_dir=self.project_dir,
            model=self.model,
            session_store=self.session_store,
            can_use_tool=self._can_use_tool,
        )

    # ── 权限回调（注入常驻执行体；ask 态触发）──

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict, context: ToolPermissionContext
    ) -> PermissionResult:
        """常驻执行体权限回调（跑在 prompt_toolkit 同一事件循环）。
        非 AskUserQuestion 直接放行不问用户；AskUserQuestion 渲染问题卡 +
        切 answer 模式，逐问题等用户输入（数字=选编号选项，文字=自定义答案）。
        """
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow()
        answers: dict[str, str] = {}
        for q in tool_input.get("questions", []):
            question_text = q.get("question", "")
            labels = [o.get("label", "") for o in q.get("options", [])]
            display = [
                f"{o.get('label', '')} — {o.get('description')}"
                if o.get("description") else o.get("label", "")
                for o in q.get("options", [])
            ]
            self.content_buffer.append_question_card(question_text, display)
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_answer = fut
            # 数字映射用纯 label（answers 值）；display 仅供卡片展示
            self._pending_options = labels
            self.tui.set_answer_mode(True)
            try:
                answer = await fut
            except asyncio.CancelledError:
                # ESC 取消整个消息：Future 被取消，拒绝本次提问（不悬挂）
                return PermissionResultDeny(message="用户取消了提问")
            finally:
                self._pending_answer = None
                self._pending_options = []
                self.tui.set_answer_mode(False)
            answers[question_text] = answer
        return PermissionResultAllow(
            updated_input={**tool_input, "answers": answers}
        )

    def _resolve_answer(self, text: str) -> None:
        """answer 模式输入 → 兑现 pending Future（纯数字=选编号选项，其余=自定义答案）。"""
        fut = self._pending_answer
        if fut is None or fut.done():
            return
        answer = text
        # isascii 守卫：isdigit 收 Unicode 数字（如 '²'）但 int() 拒收
        if text.isascii() and text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(self._pending_options):
                answer = self._pending_options[idx - 1]
        self.content_buffer.append_plain(f"回答> {answer}")
        fut.set_result(answer)

    @property
    def session(self):
        """穿透到 Governor 常驻执行体（commands.py 的 cli.session.* 零改动）。"""
        return self.governor.session

    async def run(self) -> None:
        """启动 TUI 应用（阻塞直到退出）"""
        # 检查点模式下不显示会话选择界面
        if not self.enable_undo:
            # 如果没有指定恢复的会话，显示会话选择界面
            if not self.resume_session_id and not self.continue_conversation:
                selector = SessionSelector(self.session_store, self.project_dir)
                selected_session = await selector.pick()
                if selected_session is None:
                    # 用户选择退出
                    return
                elif selected_session == NEW_SESSION:
                    # 用户选择新会话
                    pass
                else:
                    # 用户选择恢复历史会话
                    self.resume_session_id = selected_session
                    # 重建 Governor（注：Governor 暂不支持 resume，会话恢复后置）
                    await self.governor.close()
                    self.governor = self._create_session()

        # 注册输入回调
        self.tui.set_on_submit(self._on_user_input)
        # 注册取消回调
        self.tui.set_on_cancel(self._cancel_current_request)

        # 启动 Governor（含常驻执行体）
        await self.governor.start()

        # 如果用户指定了模型，显示指定的模型名；否则显示 "default" 等待首次回复后更新
        initial_model = self.model or "default"
        self.tui.model_name = initial_model

        # 渲染欢迎信息
        self._render_welcome()

        # 如果是恢复会话，显示恢复提示
        if self.resume_session_id:
            self.renderer.render_command_result(
                f"会话恢复暂未支持，已开新会话（原选: {self.resume_session_id[:8]}...）"
            )

        try:
            await self.tui.run()
        finally:
            # 确保退出时取消任何正在进行的请求
            await self._cancel_message_task()
            await self.governor.close()
            self.renderer.render_goodbye(self.session.stats.to_dict())

    async def _on_user_input(self, text: str) -> None:
        """用户输入回调（由 TuiApp 的 Buffer accept 触发）

        在 prompt_toolkit 的 asyncio loop 中运行，
        可以安全地 await SDK 调用。
        """
        if self._should_exit:
            return

        # answer 模式：pending Future 非空时输入路由给 Future，而非发新消息
        if self._pending_answer is not None and not self._pending_answer.done():
            self._resolve_answer(text)
            return

        # 判断是斜杠命令还是普通消息
        if text.startswith("/"):
            await self._handle_command(text)
        else:
            await self._handle_message(text)

    async def _handle_command(self, input_str: str) -> None:
        """处理斜杠命令"""
        parts = input_str[1:].split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        command = get_command(name)
        if command:
            await command.handler(self, args)
        else:
            self.renderer.render_command_result(
                f"未知命令: /{name}  输入 /help 查看可用命令"
            )

    async def _handle_message(self, prompt: str, *, forced_track: str | None = None) -> None:
        """处理普通聊天消息 — 发送给 Agent

        使用独立的任务来处理，以便可以取消。

        semi 模式下非命令强制的普通输入直通交互轨（forced_track="interactive"），
        由常驻执行体在对话中理解需求 + propose_plan 生成方案 + "采用方案"走交付轨。
        """
        # semi 模式：非命令强制的普通输入直通交互轨（命令层 forced_track 优先）
        if forced_track is None and self.mode == "semi":
            forced_track = "interactive"
        # 如果已有任务在运行，先取消它
        await self._cancel_message_task()

        # 创建新任务处理消息
        self._message_task = asyncio.create_task(
            self._stream_events(prompt, forced_track=forced_track)
        )

        try:
            await self._message_task
        except asyncio.CancelledError:
            # 任务被取消是正常行为
            pass

    async def _cancel_message_task(self) -> None:
        """取消正在进行的消息处理任务并等待其结束（幂等）"""
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

    def _render_welcome(self) -> None:
        """渲染欢迎信息"""
        from harness_agent import __version__
        self.renderer.render_welcome(self.project_dir, __version__)

    async def _stream_events(self, prompt: str, *, forced_track: str | None = None) -> None:
        """流式处理事件

        这是实际处理消息的方法，可以被取消。
        """
        async for event in self.governor.handle_user_input(prompt, forced_track=forced_track):
            # 检查任务是否被取消
            if asyncio.current_task().cancelled():
                break
            self.renderer.handle(event)

    def _cancel_current_request(self) -> None:
        """取消当前请求

        由 TUI 的 ESC 键触发。answer 模式同样取消整个消息：
        pending Future 一并取消（不悬挂），回调 finally 恢复输入路由与前缀。
        """
        # 0. answer 模式：取消 pending Future（回调内 await 抛 CancelledError → Deny）
        if self._pending_answer is not None and not self._pending_answer.done():
            self._pending_answer.cancel()

        # 1. 先设置 session 的取消标志（在异步迭代中会检查）
        if self.session.is_processing:
            self.session.cancel()

        # 2. 取消消息处理任务
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()

    # ── 命令回调接口（供 commands.py 调用） ──

    def request_exit(self) -> None:
        """请求退出"""
        self._should_exit = True
        self.tui.exit()

    async def reset_session(self) -> None:
        """清空会话，重建 Governor 常驻执行体"""
        # 取消正在进行的任务
        await self._cancel_message_task()

        await self.governor.rebuild()
        self.content_buffer.clear()
        self._render_welcome()

    async def switch_project(self, new_dir: str) -> None:
        """切换项目目录"""
        self.project_dir = str(Path(new_dir).resolve())
        await self.governor.rebuild(project_dir=self.project_dir)

    async def switch_model(self, model: str) -> None:
        """切换模型"""
        self.model = model
        self.tui.model_name = model
        await self.governor.rebuild(model=model)