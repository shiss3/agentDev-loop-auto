"""ChatSession — 基于 BaseAgentSession 的多轮对话会话。

在底座之上加：用 ContextProvider 构建 claude_code preset system_prompt；
保留 enable_undo 检查点模式。其余会话管理逻辑全部继承自 BaseAgentSession。

对外签名 / 行为与重构前完全一致（三个新参数 system_prompt / permission_mode /
can_use_tool 走默认，等价于旧 ChatSession）。
"""

from __future__ import annotations

from claude_agent_sdk import SessionStore

from autoloop_agent.context.provider import ContextProvider, SystemPromptType
from autoloop_agent.core.base_session import BaseAgentSession


_BASE_SYSTEM_PROMPT = """你是 AutoLoop 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。

## 路径说明
- 所有相对路径（如 docs/1.md）都基于当前工作目录（cwd）解析。
- 如果用户问\"项目中某个文件\"，先用 Glob 或 Bash 确认项目根目录结构，
  再按 cwd 定位文件。不要跨 cwd 范围去寻找项目外的路径。"""


class ChatSession(BaseAgentSession):
    """多轮对话会话 — Claude Code 专属基提示 + ContextProvider 驱动。

    使用方式：

        session = ChatSession(project_dir="./my-app")
        await session.start()

        async for event in session.send("重构 user.py"):
            renderer.handle(event)

        await session.close()

    三个新参数（system_prompt / permission_mode / can_use_tool）不接受 ——
    走 BaseAgentSession 默认（None / "acceptEdits" / None），与重构前行为一致。
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
        # 三个新参数走默认：system_prompt=None / permission_mode="acceptEdits" / can_use_tool=None
        # → ChatSession 行为与重构前完全一致
        super().__init__(
            project_dir=project_dir,
            model=model,
            max_turns=max_turns,
            context_provider=context_provider,
            allowed_tools=allowed_tools,
            session_store=session_store,
            resume_session_id=resume_session_id,
            continue_conversation=continue_conversation,
            enable_undo=enable_undo,
        )

    def _resolve_system_prompt(self) -> SystemPromptType:
        """覆盖底座钩子：用 ContextProvider 构建 claude_code preset system_prompt。"""
        return self.context_provider.build_system_prompt(
            base_prompt=_BASE_SYSTEM_PROMPT,
            project_dir=self.project_dir,
        )
