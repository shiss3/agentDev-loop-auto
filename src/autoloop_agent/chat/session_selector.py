"""SessionSelector — 会话恢复选择界面

从 ChatCLI 抽出的独立组件，职责单一：
  在启动时列出历史会话，让用户选择恢复哪个会话 / 新建 / 退出。

输入：session_store（用于列出会话）+ project_dir（计算 project_key）
输出：pick() 返回
  - "__new__": 用户选择新建会话
  - session_id: 用户选择恢复的历史会话
  - None: 用户选择退出

绝不做的事：
- ❌ 管理 ChatSession / SDK 会话生命周期（交给 ChatCLI）
- ❌ 渲染聊天内容（交给 Renderer）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from prompt_toolkit.key_binding import KeyBindings

from claude_agent_sdk import project_key_for_directory

logger = logging.getLogger(__name__)

# 标志值：用户选择新建会话
NEW_SESSION = "__new__"

# 菜单最多展示的历史会话数
_MAX_MENU_ITEMS = 10
# 标题最大显示长度
_MAX_TITLE_LEN = 30


class SessionSelector:
    """会话恢复选择界面"""

    def __init__(self, session_store: Any, project_dir: str) -> None:
        self.session_store = session_store
        self.project_dir = project_dir
        # 菜单临时状态（仅菜单运行期间有效）
        self._menu_total_options = 0
        self._selected_index = 0

    async def pick(self) -> str | None:
        """显示会话选择界面并返回用户选择

        逻辑：
        - 无历史会话 → 直接返回 NEW_SESSION，跳过选择界面
        - 有历史会话 → 显示选择菜单，让用户明确选择
        """
        try:
            project_key = project_key_for_directory(self.project_dir)
            summaries = await self.session_store.list_session_summaries(project_key)
            logger.debug(f"Found {len(summaries)} sessions for project_key={project_key}")
        except Exception as e:
            logger.warning(f"Failed to list sessions: {e}")
            summaries = []

        # 如果没有历史会话，直接进入新会话
        if not summaries:
            logger.debug("No sessions found, starting new session")
            return NEW_SESSION

        # 有历史会话，显示选择界面
        return await self._render_session_menu(summaries)

    async def _render_session_menu(self, summaries: list[Any]) -> str | None:
        """渲染会话选择菜单

        使用 prompt_toolkit 的 Application 实现全屏菜单。
        """
        options = self._build_options(summaries)

        # 菜单选项总数（包含新建会话选项）
        self._menu_total_options = len(options)
        self._selected_index = 0

        try:
            return await self._run_menu_app(options)
        except Exception as e:
            logger.error(f"Session selector error: {e}", exc_info=True)
            return self._run_fallback_menu(options)

    def _build_options(self, summaries: list[Any]) -> list[tuple[str, str, str]]:
        """构建菜单选项列表：(display_text, value, time_str)"""
        options: list[tuple[str, str, str]] = []

        # 选项 1: 新建会话
        options.append(("🆕 开始新对话", NEW_SESSION, ""))

        # 选项 2-N: 历史会话（最多显示 _MAX_MENU_ITEMS 个）
        for summary in summaries[:_MAX_MENU_ITEMS]:
            session_id = summary.get("session_id", "")
            mtime = summary.get("mtime", 0)
            data = summary.get("data", {})

            # 提取标题（兼容多种字段名）
            # 优先级：ai_title > summary_hint > first_prompt
            title = (
                data.get("ai_title")
                or data.get("summary_hint")
                or data.get("first_prompt", "未命名会话")
            )
            if len(title) > _MAX_TITLE_LEN:
                title = title[:_MAX_TITLE_LEN] + "..."

            time_str = self._format_session_time(mtime)
            options.append((f"{title}", session_id, time_str))

        return options

    async def _run_menu_app(self, options: list[tuple[str, str, str]]) -> str | None:
        """用 prompt_toolkit Application 运行全屏菜单"""
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style

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

        content = FormattedTextControl(text=get_menu_text, focusable=True)
        layout = Layout(Window(content=content))

        kb = KeyBindings()

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

        app = Application(
            layout=layout,
            key_bindings=kb,
            style=menu_style,
            full_screen=False,
            mouse_support=True,
        )

        return await app.run_async()

    def _run_fallback_menu(self, options: list[tuple[str, str, str]]) -> str | None:
        """降级到简单的 print/input 菜单（Application 失败时）"""
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
                return NEW_SESSION

            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][1]
            return NEW_SESSION
        except (ValueError, EOFError):
            return NEW_SESSION

    @staticmethod
    def _format_session_time(mtime: int) -> str:
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
