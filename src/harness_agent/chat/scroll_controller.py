"""ScrollController — TUI 内容区滚动逻辑

把"虚拟光标行号"驱动 vertical_scroll 的全部状态与逻辑从 TuiApp 中抽离：

  prompt_toolkit 的 Window._scroll 每帧会根据 ui_content.cursor_position.y
  重写 vertical_scroll。我们通过 get_cursor_position 提供这个 y，
  _scroll 就会自动把目标行带入可视区域，以此控制滚动位置。

  跟随模式判定：cursor_line >= line_count - 1

绝不做的事：
- ❌ 了解 ChatEvent / SDK 会话
- ❌ 直接持有 Application（通过注入的 invalidate 回调触发重绘）
"""

from __future__ import annotations

from typing import Callable

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import FormattedTextControl


class _ScrollableFTControl(FormattedTextControl):
    """支持鼠标滚轮 _scroll_down/_scroll_up 调用 move_cursor_* 的 FormattedTextControl

    prompt_toolkit Window._scroll_down/_scroll_up（containers.py:2559/2572）
    会调 content.move_cursor_down()/move_cursor_up()。FormattedTextControl
    默认不实现这两个方法 → 鼠标滚轮事件无效。本子类把它们路由到外部回调，
    使外部可以通过修改"虚拟光标行号"驱动滚动。
    """

    def __init__(self, *args, on_cursor_down=None, on_cursor_up=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_cursor_down = on_cursor_down
        self._on_cursor_up = on_cursor_up

    def move_cursor_down(self) -> None:
        if self._on_cursor_down:
            self._on_cursor_down()

    def move_cursor_up(self) -> None:
        if self._on_cursor_up:
            self._on_cursor_up()


class ScrollController:
    """用"虚拟光标行号"驱动 vertical_scroll 的滚动控制器

    Args:
        get_fragments: 返回内容区 fragments 的回调（通常来自 ContentBuffer）
        invalidate:    触发 TUI 重绘的回调
    """

    # 翻页步长（PageUp / PageDown）
    PAGE_STEP = 20

    def __init__(
        self,
        get_fragments: Callable[[], StyleAndTextTuples],
        invalidate: Callable[[], None],
    ) -> None:
        self._get_fragments = get_fragments
        self._invalidate = invalidate
        # 虚拟光标行号 / 内容总行数
        self.cursor_line: int = 0
        self.line_count: int = 1

    # ── 提供给 FormattedTextControl 的回调 ──

    def get_content_fragments(self) -> StyleAndTextTuples:
        """内容区文本回调：取 fragments 同时统计总行数（用于光标 clamp）"""
        fragments = self._get_fragments()
        # fragment 元组结构是 (style, text) 或 (style, text, handler)
        text = "".join(item[1] for item in fragments)
        self.line_count = max(1, text.count("\n") + 1)
        return fragments

    def get_cursor_position(self) -> Point:
        """提供给 FormattedTextControl 的虚拟光标位置

        prompt_toolkit Window._scroll 会根据这个 y 值反算 vertical_scroll，
        把目标行带入可视区域 → 我们以此控制滚动位置。
        """
        y = max(0, min(self.cursor_line, self.line_count - 1))
        return Point(x=0, y=y)

    # ── 滚动动作 ──

    def is_following(self) -> bool:
        """虚拟光标是否在内容末尾（即处于自动跟随状态）"""
        return self.cursor_line >= self.line_count - 1

    def cursor_down_one(self) -> None:
        """鼠标滚轮向下 → 虚拟光标下移一行"""
        self.cursor_line = min(self.line_count - 1, self.cursor_line + 1)

    def cursor_up_one(self) -> None:
        """鼠标滚轮向上 → 虚拟光标上移一行"""
        self.cursor_line = max(0, self.cursor_line - 1)

    def page_up(self) -> None:
        """PageUp: 向上翻 PAGE_STEP 行（暂停自动跟随）"""
        self.cursor_line = max(0, self.cursor_line - self.PAGE_STEP)
        self._invalidate()

    def page_down(self) -> None:
        """PageDown: 向下翻 PAGE_STEP 行（到达末尾即恢复自动跟随）"""
        self.cursor_line = min(self.line_count - 1, self.cursor_line + self.PAGE_STEP)
        self._invalidate()

    def to_home(self) -> None:
        """Home: 跳到内容顶部"""
        self.cursor_line = 0
        self._invalidate()

    def to_end(self) -> None:
        """End: 跳到内容末尾，恢复自动跟随"""
        self.cursor_line = max(0, self.line_count - 1)
        self._invalidate()

    def scroll_to_end(self) -> None:
        """强制将虚拟光标滚动到内容末尾

        在用户输入后立即调用，确保在 Agent 开始输出之前，
        滚动位置已经在末尾，之后的内容会自动跟随。
        """
        # 先刷新 line_count（确保基于最新的内容）
        self.get_content_fragments()
        self.cursor_line = max(0, self.line_count - 1)
        self._invalidate()

    def on_content_change(self) -> None:
        """ContentBuffer 变更回调 → 跟随模式下把虚拟光标钉在末尾，触发重绘

        ⚠ 关键：必须在刷新 line_count **之前** 判断 is_following，否则首次内容追加
        会把 line_count 从 1 跳到 N，cursor=0 < N-1 → 永远脱离跟随。

        ⚠ 不能用 10**9 这种哨兵值 —— 那样 is_following 永远为 True，
        PageUp 即便修改了 cursor_line（如 10^9 - 20），下次 spinner tick / status 更新
        触发的 on_content_change 会立刻把它钉回末尾，视觉上"完全无法滚动"。
        """
        # 1. 用 **旧** line_count 判断当前是否处于跟随状态
        was_following = self.is_following()

        # 2. 刷新 line_count（取一次最新 fragments）
        self.get_content_fragments()

        # 3. 跟随状态下，把虚拟光标钉在 **新** 末尾行（真实行号，非哨兵）
        if was_following:
            self.cursor_line = max(0, self.line_count - 1)

        self._invalidate()
