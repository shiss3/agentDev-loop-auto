"""ChatCLI — REPL 聊天主循环

Phase 2.5 (TUI 版):
  使用 TuiApp 全屏 TUI 替代 PromptSession + Rich Console。
  用户输入通过 TuiApp 的 Buffer accept 回调获取。
  所有输出通过 ContentBuffer → prompt_toolkit 渲染。

职责：
1. 管理 TuiApp / ChatRenderer / ChatSession 的生命周期
2. 分发用户输入（消息 vs 斜杠命令）
3. 处理退出信号
4. 处理请求取消
5. 会话恢复：启动时显示历史会话选择界面
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from prompt_toolkit.key_binding import KeyBindings

from harness_agent.chat.content_buffer import ContentBuffer
from harness_agent.chat.session_store import FileSessionStore, create_session_store
from harness_agent.chat.tui_app import TuiApp
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.session import ChatSession

# 导入 commands 以注册所有内置斜杠命令（必须保留此 import）
from harness_agent.chat import commands as _commands  # noqa: F401
from harness_agent.chat.commands import get_command
from harness_agent.context.provider import ContextProvider, DefaultContextProvider

# SDK 的 project_key 转换函数
from claude_agent_sdk import project_key_for_directory

logger = logging.getLogger(__name__)


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
        context_provider: ContextProvider | None = None,
        history_file: str | None = None,
        session_store: FileSessionStore | None = None,
        resume_session_id: str | None = None,
        continue_conversation: bool = False,
        enable_undo: bool = False,  # 开启检查点模式（禁用会话恢复）
    ) -> None:
        self.project_dir = str(Path(project_dir).resolve())
        self.model = model
        self.context_provider = context_provider or DefaultContextProvider()

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
        self.session = self._create_session()

        # 状态
        self._should_exit = False
        # 当前消息处理的任务引用（用于取消）
        self._message_task: asyncio.Task | None = None

    def _create_session(self) -> ChatSession:
        return ChatSession(
            project_dir=self.project_dir,
            model=self.model,
            context_provider=self.context_provider,
            session_store=self.session_store,
            resume_session_id=self.resume_session_id,
            continue_conversation=self.continue_conversation,
            enable_undo=self.enable_undo,
        )

    async def run(self) -> None:
        """启动 TUI 应用（阻塞直到退出）"""
        # 检查点模式下不显示会话选择界面
        if not self.enable_undo:
            # 如果没有指定恢复的会话，显示会话选择界面
            if not self.resume_session_id and not self.continue_conversation:
                selected_session = await self._show_session_selector()
                if selected_session is None:
                    # 用户选择退出
                    return
                elif selected_session == "__new__":
                    # 用户选择新会话
                    pass
                else:
                    # 用户选择恢复历史会话
                    self.resume_session_id = selected_session
                    # 重新创建会话以使用新的 session_id
                    await self.session.close()
                    self.session = self._create_session()

        # 注册输入回调
        self.tui.set_on_submit(self._on_user_input)
        # 注册取消回调
        self.tui.set_on_cancel(self._cancel_current_request)

        # 启动 SDK 会话
        await self.session.start()

        # 如果用户指定了模型，显示指定的模型名；否则显示 "default" 等待首次回复后更新
        initial_model = self.model or "default"
        self.tui.model_name = initial_model

        # 渲染欢迎信息
        from harness_agent import __version__
        self.renderer.render_welcome(self.project_dir, __version__)

        # 如果是恢复会话，显示恢复提示
        if self.resume_session_id:
            self.renderer.render_command_result(f"已恢复会话: {self.resume_session_id[:8]}...")

        try:
            await self.tui.run()
        finally:
            # 确保退出时取消任何正在进行的请求
            if self._message_task and not self._message_task.done():
                self._message_task.cancel()
                try:
                    await self._message_task
                except asyncio.CancelledError:
                    pass

            await self.session.close()
            self.renderer.render_goodbye(self.session.stats.to_dict())

    async def _on_user_input(self, text: str) -> None:
        """用户输入回调（由 TuiApp 的 Buffer accept 触发）

        在 prompt_toolkit 的 asyncio loop 中运行，
        可以安全地 await SDK 调用。
        """
        if self._should_exit:
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

    async def _handle_message(self, prompt: str) -> None:
        """处理普通聊天消息 — 发送给 Agent

        使用独立的任务来处理，以便可以取消。
        """
        # 如果已有任务在运行，先取消它
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

        # 创建新任务处理消息
        self._message_task = asyncio.create_task(self._stream_events(prompt))

        try:
            await self._message_task
        except asyncio.CancelledError:
            # 任务被取消是正常行为
            pass

    async def _stream_events(self, prompt: str) -> None:
        """流式处理事件

        这是实际处理消息的方法，可以被取消。
        """
        async for event in self.session.send(prompt):
            # 检查任务是否被取消
            if asyncio.current_task().cancelled():
                break
            self.renderer.handle(event)

    def _cancel_current_request(self) -> None:
        """取消当前请求

        由 TUI 的 ESC 键触发。
        """
        # 1. 先设置 session 的取消标志（在异步迭代中会检查）
        if self.session.is_processing:
            self.session.cancel()

        # 2. 取消消息处理任务
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()

    # ── 会话选择器 ──

    async def _show_session_selector(self) -> str | None:
        """显示会话选择界面

        逻辑：
        - 无历史会话 → 直接返回 "__new__"，跳过选择界面
        - 有历史会话 → 显示选择菜单，让用户明确选择

        Returns:
            - "__new__": 用户选择新建会话
            - session_id: 用户选择恢复的历史会话
            - None: 用户选择退出
        """
        try:
            # 将 project_dir 转换为 project_key
            project_key = project_key_for_directory(self.project_dir)
            # 获取历史会话列表
            summaries = await self.session_store.list_session_summaries(project_key)
        except Exception as e:
            logger.warning(f"Failed to list sessions: {e}")
            summaries = []

        # 如果没有历史会话，直接进入新会话
        if not summaries:
            return "__new__"

        # 有历史会话，显示选择界面
        return await self._render_session_menu(summaries)

    async def _render_session_menu(
        self,
        summaries: list[Any],
    ) -> str | None:
        """渲染会话选择菜单

        使用 prompt_toolkit 的 PromptSession 实现简单的菜单选择。
        """
        # 构建菜单选项
        options: list[tuple[str, str, str]] = []  # (display_text, value, time_str)

        # 选项 1: 新建会话
        options.append(("🆕 开始新对话", "__new__", ""))

        # 选项 2-N: 历史会话（最多显示 10 个）
        for summary in summaries[:10]:
            session_id = summary.get("session_id", "")
            mtime = summary.get("mtime", 0)
            data = summary.get("data", {})

            # 提取标题
            title = data.get("summary_hint", "未命名会话")
            if len(title) > 30:
                title = title[:30] + "..."

            # 格式化时间
            time_str = self._format_session_time(mtime)

            options.append((f"{title}", session_id, time_str))

        # 菜单选项总数（包含新建会话选项）
        self._menu_total_options = len(options)

        # 当前选中的索引
        self._selected_index = 0

        # 构建 key bindings（稍后重新构建以支持刷新）
        # TODO：

        # 使用 prompt_toolkit 的 Application 实现全屏菜单
        try:
            from prompt_toolkit.application import Application
            from prompt_toolkit.layout import Layout, Window
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.styles import Style

            # 菜单样式
            menu_style = Style.from_dict({
                "header": "bold cyan",
                "selected": "bold green",
                "item": "",
                "time": "gray",
                "hint": "dim",
            })

            def get_menu_text():
                """生成菜单显示文本"""
                lines = []
                lines.append(("class:header", "  📋 最近对话\n\n"))

                for i, (display, value, time_str) in enumerate(options):
                    if i == self._selected_index:
                        lines.append(("class:selected", f"  ➤ [{i + 1}] {display}"))
                        if time_str:
                            lines.append(("class:time", f"   {time_str}\n"))
                        else:
                            lines.append(("", "\n"))
                    else:
                        lines.append(("class:item", f"    [{i + 1}] {display}"))
                        if time_str:
                            lines.append(("class:time", f"   {time_str}\n"))
                        else:
                            lines.append(("", "\n"))

                lines.append(("", "\n"))
                # 新手引导提示
                lines.append(("class:hint", "  ┌─────────────────────────────────────────┐\n"))
                lines.append(("class:hint", "  │ ↑↓/j/k 移动  │ Enter 选择  │ 1-9 快捷 │\n"))
                lines.append(("class:hint", "  │ ESC/q 退出    │ 继续最近会话请直接 Enter │\n"))
                lines.append(("class:hint", "  └─────────────────────────────────────────┘"))
                return lines

            # 创建布局
            content = FormattedTextControl(text=get_menu_text, focusable=True)
            layout = Layout(Window(content=content))

            # 创建 key bindings（在 app 创建前，以便引用）
            kb = KeyBindings()

            def _invalidate_and_exit(result):
                """保存选择结果并退出"""
                event.app.exit(result=result)

            @kb.add("up")
            @kb.add("k")
            def _up(event):
                if self._selected_index > 0:
                    self._selected_index -= 1

            @kb.add("down")
            @kb.add("j")
            def _down(event):
                if self._selected_index < self._menu_total_options - 1:
                    self._selected_index += 1

            @kb.add("enter")
            def _select(event):
                event.app.exit(result=options[self._selected_index][1])

            @kb.add("c-c")
            @kb.add("escape")
            @kb.add("q")
            def _cancel(event):
                event.app.exit(result=None)

            for num in range(1, min(10, len(options) + 1)):
                def _quick_select(event, idx=num - 1):
                    event.app.exit(result=options[idx][1])
                kb.add(str(num))(_quick_select)

            # 创建应用
            app = Application(
                layout=layout,
                key_bindings=kb,
                style=menu_style,
                full_screen=False,
                mouse_support=True,
            )

            # 运行并获取结果
            result = await app.run_async()
            return result

        except Exception as e:
            logger.warning(f"Session selector error: {e}, falling back to simple input")

            # 降级到简单的输入方式
            print("\n  📋 最近对话\n")
            for i, (display, value, time_str) in enumerate(options):
                time_display = f"   {time_str}" if time_str else ""
                print(f"    [{i + 1}] {display}{time_display}")
            print("\n  ┌─────────────────────────────────────────┐")
            print("  │ ↑↓ 移动  │ Enter 选择  │ 1-9 快捷选择   │")
            print("  │ ESC/q 退出  │ 直接 Enter 继续最近会话    │")
            print("  └─────────────────────────────────────────┘")
            print(f"\n  请选择 (1-{len(options)}) 或直接 Enter 新建会话: ", end="")

            try:
                choice = input().strip()
                if not choice:
                    return "__new__"

                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    return options[idx][1]
                return "__new__"
            except (ValueError, EOFError):
                return "__new__"

    def _format_session_time(self, mtime: int) -> str:
        """格式化会话时间

        Args:
            mtime: Unix 时间戳（毫秒）

        Returns:
            格式化的时间字符串，如 "06-11 14:30"
        """
        if mtime <= 0:
            return ""

        try:
            dt = datetime.fromtimestamp(mtime / 1000)
            return dt.strftime("%m-%d %H:%M")
        except Exception:
            return ""

    # ── 命令回调接口（供 commands.py 调用） ──

    def request_exit(self) -> None:
        """请求退出"""
        self._should_exit = True
        self.tui.exit()

    async def reset_session(self) -> None:
        """清空会话，重建 ChatSession"""
        # 取消正在进行的任务
        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

        await self.session.close()
        self.session = self._create_session()
        await self.session.start()
        self.content_buffer.clear()
        from harness_agent import __version__
        self.renderer.render_welcome(self.project_dir, __version__)

    async def switch_project(self, new_dir: str) -> None:
        """切换项目目录"""
        self.project_dir = str(Path(new_dir).resolve())
        await self.reset_session()

    async def switch_model(self, model: str) -> None:
        """切换模型"""
        self.model = model
        self.tui.model_name = model
        await self.reset_session()