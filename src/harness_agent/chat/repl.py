"""ChatCLI — REPL 聊天主循环

Phase 2.5 (TUI 版):
  使用 TuiApp 全屏 TUI 替代 PromptSession + Rich Console。
  用户输入通过 TuiApp 的 Buffer accept 回调获取。
  所有输出通过 ContentBuffer → prompt_toolkit 渲染。

职责：
1. 管理 TuiApp / ChatRenderer / ChatSession 的生命周期
2. 分发用户输入（消息 vs 斜杠命令）
3. 处理退出信号
"""

from __future__ import annotations

from pathlib import Path

from harness_agent.chat.content_buffer import ContentBuffer
from harness_agent.chat.tui_app import TuiApp
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.session import ChatSession

# 导入 commands 以注册所有内置斜杠命令（必须保留此 import）
from harness_agent.chat import commands as _commands  # noqa: F401
from harness_agent.chat.commands import get_command
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


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
    ) -> None:
        self.project_dir = str(Path(project_dir).resolve())
        self.model = model
        self.context_provider = context_provider or DefaultContextProvider()

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

    def _create_session(self) -> ChatSession:
        return ChatSession(
            project_dir=self.project_dir,
            model=self.model,
            context_provider=self.context_provider,
        )

    async def run(self) -> None:
        """启动 TUI 应用（阻塞直到退出）"""
        # 注册输入回调
        self.tui.set_on_submit(self._on_user_input)

        # 启动 SDK 会话
        await self.session.start()

        # 渲染欢迎信息
        from harness_agent import __version__
        self.renderer.render_welcome(self.project_dir, __version__)

        try:
            await self.tui.run()
        finally:
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
        """处理普通聊天消息 — 发送给 Agent"""
        async for event in self.session.send(prompt):
            self.renderer.handle(event)

    # ── 命令回调接口（供 commands.py 调用） ──

    def request_exit(self) -> None:
        """请求退出"""
        self._should_exit = True
        self.tui.exit()

    async def reset_session(self) -> None:
        """清空会话，重建 ChatSession"""
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
