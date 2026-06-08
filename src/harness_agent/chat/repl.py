"""ChatCLI — REPL 聊天主循环

职责：
1. 管理输入循环（prompt → 解析 → 分发）
2. 区分普通消息和斜杠命令
3. 管理 ChatSession 的生命周期
4. 处理 Ctrl+C / Ctrl+D 等信号
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

# 导入 commands 以注册所有内置斜杠命令（必须保留此 import）
from harness_agent.chat import commands as _commands  # noqa: F401
from harness_agent.chat.commands import get_command
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.session import ChatSession
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


class ChatCLI:
    """REPL 聊天主循环

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

        # 组件
        self.console = Console()
        self.renderer = ChatRenderer(console=self.console)
        self.session = self._create_session()

        # 输入历史
        history_path = history_file or str(
            Path.home() / ".harness" / "chat_history"
        )
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        self._prompt_session: PromptSession = PromptSession(
            history=FileHistory(history_path),
            auto_suggest=AutoSuggestFromHistory(),
        )

        # 状态
        self._should_exit = False

    def _create_session(self) -> ChatSession:
        return ChatSession(
            project_dir=self.project_dir,
            model=self.model,
            context_provider=self.context_provider,
        )

    async def run(self) -> None:
        """REPL 主循环"""
        self.renderer.render_welcome(self.project_dir)

        await self.session.start()

        try:
            while not self._should_exit:
                try:
                    # 获取用户输入
                    user_input = await self._get_input()

                    if user_input is None:
                        # Ctrl+D → 退出
                        break

                    user_input = user_input.strip()
                    if not user_input:
                        continue

                    # 判断是斜杠命令还是普通消息
                    if user_input.startswith("/"):
                        await self._handle_command(user_input)
                    else:
                        await self._handle_message(user_input)

                except KeyboardInterrupt:
                    # Ctrl+C → 中断当前，不退出
                    self.console.print("\n[dim]已中断[/dim]")
                    continue

        finally:
            await self.session.close()
            self.renderer.render_goodbye(self.session.stats.to_dict())

    async def _get_input(self) -> str | None:
        """获取用户输入（带历史 / 自动补全）"""
        try:
            result = await self._prompt_session.prompt_async("You > ")
            return result
        except EOFError:
            return None

    async def _handle_command(self, input_str: str) -> None:
        """处理斜杠命令"""
        # 解析: "/command arg1 arg2" → name="command", args="arg1 arg2"
        parts = input_str[1:].split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        command = get_command(name)
        if command:
            await command.handler(self, args)
        else:
            self.renderer.render_command_result(
                f"[yellow]未知命令: /{name}[/yellow]  输入 /help 查看可用命令"
            )

    async def _handle_message(self, prompt: str) -> None:
        """处理普通聊天消息 — 发送给 Agent"""
        async for event in self.session.send(prompt):
            self.renderer.handle(event)

    # ── 命令回调接口（供 commands.py 调用） ──

    def request_exit(self) -> None:
        self._should_exit = True

    async def reset_session(self) -> None:
        """清空会话，重建 ChatSession"""
        await self.session.close()
        self.session = self._create_session()
        await self.session.start()

    async def switch_project(self, new_dir: str) -> None:
        """切换项目目录"""
        self.project_dir = str(Path(new_dir).resolve())
        await self.reset_session()

    async def switch_model(self, model: str) -> None:
        """切换模型"""
        self.model = model
        await self.reset_session()
