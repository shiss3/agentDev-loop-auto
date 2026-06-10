"""ContentBuffer — 内容区缓冲区

职责：
1. 累积对话内容（用户输入、Agent 文本、工具调用、错误）
2. 管理 Spinner 动画（启动/停止/帧更新）
3. 提供 prompt_toolkit FormattedTextControl 需要的 callable
4. Rich markup → prompt_toolkit fragment 转换

渲染策略：
  流式阶段：纯文本直接追加（快速、无延迟）
  Turn End 后：可选对累积的 Markdown 文本做一次精渲染替换

绝不做的事：
- ❌ 直接操作终端
- ❌ 管理 SDK 会话
- ❌ 了解 ChatEvent 类型
"""

from __future__ import annotations

from io import StringIO
from typing import Callable

from rich.console import Console
from prompt_toolkit.formatted_text import (
    ANSI,
    FormattedText,
    StyleAndTextTuples,
    to_formatted_text,
)


# Spinner 帧序列（Unicode braille dots）
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 样式常量
_STYLE_USER_PREFIX = "bold"
_STYLE_TOOL_LINE = "dim"
_STYLE_TOOL_OK = "ansigreen"
_STYLE_TOOL_FAIL = "ansired"
_STYLE_SPINNER = "ansicyan dim"
_STYLE_ERROR = "ansired bold"


def _markup_to_fragments(markup: str) -> StyleAndTextTuples:
    """将 Rich markup 转换为 prompt_toolkit fragment 列表

    通过 Rich Console 渲染为 ANSI，再由 prompt_toolkit 解析。
    """
    buf = StringIO()
    console = Console(
        file=buf, force_terminal=True, width=500, legacy_windows=False
    )
    console.print(markup, end="")
    ansi_str = buf.getvalue()
    if not ansi_str:
        return FormattedText([("", "")])
    return to_formatted_text(ANSI(ansi_str))


class ContentBuffer:
    """内容区缓冲区

    内部维护 _lines: 每行是一个 StyleAndTextTuples 列表。
    Spinner 占用一个虚拟行（_spinner_line），启动时追加，停止时移除。

    get_formatted_text() 是一个 callable，供 FormattedTextControl 使用。
    每次调用返回当前所有内容的扁平化 fragment 列表。
    """

    def __init__(self) -> None:
        self._lines: list[StyleAndTextTuples] = []
        self._spinner_active: bool = False
        self._spinner_text: str = "思考中..."
        self._spinner_frame: int = 0
        self._on_change: Callable[[], None] | None = None

    # ── 变更通知 ──

    def set_on_change(self, callback: Callable[[], None]) -> None:
        """设置内容变更回调（TuiApp 用来触发 invalidate）"""
        self._on_change = callback

    def _notify(self) -> None:
        if self._on_change:
            self._on_change()

    # ── 内容追加 ──

    def append_text(self, text: str) -> None:
        """追加纯文本（无格式，用于 Agent 流式输出）"""
        if text:
            self._lines.append(FormattedText([("", text)]))
            self._notify()

    def append_marked(self, markup: str) -> None:
        """追加 Rich markup 格式文本"""
        fragments = _markup_to_fragments(markup)
        if fragments:
            self._lines.append(fragments)
            self._notify()

    def append_user_input(self, text: str) -> None:
        """追加用户输入行（灰色背景 + You > 前缀 + 正文）"""
        self._lines.append(
            FormattedText([
                ("class:user-message bold", "You > "),
                ("class:user-message", text),
            ])
        )
        self._notify()

    def append_tool_line(
        self,
        icon: str,
        index: int,
        tool_name: str,
        summary: str,
        status: str = "ok",
    ) -> None:
        """追加工具调用单行

        Args:
            icon: 工具图标（📖/✏️/📝/⚡/🔍/📂）
            index: 全局工具调用序号
            tool_name: 工具名称
            summary: 输入摘要
            status: "ok" | "fail" | "running"
        """
        status_icon = {
            "ok": "✓",
            "fail": "✗",
            "running": "...",
        }.get(status, "")

        style_map = {
            "ok": _STYLE_TOOL_OK,
            "fail": _STYLE_TOOL_FAIL,
            "running": _STYLE_TOOL_LINE,
        }
        status_style = style_map.get(status, _STYLE_TOOL_LINE)

        self._lines.append(
            FormattedText([
                (_STYLE_TOOL_LINE, f"  {icon} [{index}] "),
                ("bold", f"{tool_name}"),
                (_STYLE_TOOL_LINE, f" {summary} "),
                (status_style, status_icon),
            ])
        )
        self._notify()

    def append_blank_line(self) -> None:
        """追加空行"""
        self._lines.append(FormattedText([("", "")]))
        self._notify()

    def append_error(self, error_text: str) -> None:
        """追加错误信息"""
        self._lines.append(
            FormattedText([
                (_STYLE_ERROR, f"💥 {error_text[:500]}"),
            ])
        )
        self._notify()

    def append_plain(self, text: str) -> None:
        """追加命令结果等辅助文本（无格式，带缩进）"""
        if text:
            self._lines.append(FormattedText([("dim", f"  {text}")]))
            self._notify()

    # ── Spinner 管理 ──

    def start_spinner(self, text: str = "思考中...") -> None:
        """启动 Spinner 动画"""
        self.stop_spinner()
        self._spinner_text = text
        self._spinner_active = True
        self._spinner_frame = 0
        self._notify()

    def stop_spinner(self) -> None:
        """停止 Spinner 动画"""
        if self._spinner_active:
            self._spinner_active = False
            self._notify()

    def tick_spinner(self) -> None:
        """推进 Spinner 到下一帧（由定时器调用）"""
        if self._spinner_active:
            self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
            self._notify()

    @property
    def spinner_active(self) -> bool:
        return self._spinner_active

    # ── prompt_toolkit 接口 ──

    def get_formatted_text(self) -> StyleAndTextTuples:
        """返回所有内容的扁平化 fragment 列表

        供 FormattedTextControl 的 text=callable 使用。
        每行之间用 \\n 连接，Spinner 行在末尾（如果激活）。
        """
        result: StyleAndTextTuples = []

        for i, line in enumerate(self._lines):
            result.extend(line)
            if i < len(self._lines) - 1 or self._spinner_active:
                result.append(("", "\n"))

        # Spinner 行
        if self._spinner_active:
            frame = _SPINNER_FRAMES[self._spinner_frame]
            result.extend([
                (_STYLE_SPINNER, f"  {frame} {self._spinner_text}"),
            ])

        # prompt_toolkit 要求非空，至少返回一个空 fragment
        if not result:
            return FormattedText([("", "")])

        return result

    # ── 辅助 ──

    def clear(self) -> None:
        """清空所有内容"""
        self._lines.clear()
        self._spinner_active = False
        self._notify()

    @property
    def line_count(self) -> int:
        """当前内容行数（不含 spinner）"""
        return len(self._lines)
