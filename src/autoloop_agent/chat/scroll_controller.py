"""ScrollController - TUI 内容区滚动逻辑
用 vertical_scroll 值(逻辑行)直接驱动滚动,绕过 prompt_toolkit 的
cursor-driven clamp。
核心问题(prompt_toolkit 3.0.52, containers.py:2316-2435):
  Window._scroll_when_linewrapping 每帧基于 ui_content.cursor_position 把
  vertical_scroll clamp 到 [get_min_vertical_scroll, get_max_vertical_scroll]
  (即"让 cursor 行可见"的区间)。本项目内容区是只读 FormattedTextControl,
  cursor 是装饰性的,这个 clamp 会让 vertical_scroll 无法离开"末尾可见"位置 ->
  PageUp/滚轮上滚无效、长行行尾看不到。
方案 B:
- _ScrollableWindow 重写 Window._scroll:vertical_scroll 完全由 ScrollController
  控制,只 clamp 到 [0, topmost_visible],不基于 cursor。
- _ScrollableWindow 重写 _scroll_up/_scroll_down:滚轮直接调
  ScrollController.scroll_up/down,不走 cursor 逻辑。
- following=True 时 _scroll 把 vertical_scroll 钉到 topmost(贴底),新内容自动跟随;
  末行为超长行时 vertical_scroll_2 滚到行尾(让最新输出可见)。
- 用户上滚(PageUp/滚轮)置 following=False,vertical_scroll 由 scroll_offset 驱动;
  滚到底(clamped >= topmost)自动恢复 following。
绝不做的事:
- ❌ 了解 ChatEvent / SDK 会话
- ❌ 直接持有 Application(通过注入的 invalidate 回调触发重绘)
"""
from __future__ import annotations
from typing import Callable
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import Window
class _ScrollableWindow(Window):
    """重写 _scroll / _scroll_up / _scroll_down 的 Window 子类
    接管 vertical_scroll,使其不受 prompt_toolkit cursor-driven clamp 影响。
    """
    def __init__(self, *args, scroll_ctl: "ScrollController", **kwargs):
        super().__init__(*args, **kwargs)
        self._scroll_ctl = scroll_ctl
    def _scroll(self, ui_content, width: int, height: int) -> None:
        """vertical_scroll 由 ScrollController 控制,只做边界 clamp
        复用 prompt_toolkit containers.py:2405-2419 的 topmost_visible 算法
        (贴底时最顶可见逻辑行 = vertical_scroll 上界)。
        """
        self.horizontal_scroll = 0
        if width <= 0:
            self.vertical_scroll = 0
            self.vertical_scroll_2 = 0
            return
        line_count = ui_content.line_count
        if line_count == 0:
            self.vertical_scroll = 0
            self.vertical_scroll_2 = 0
            return
        def get_line_height(lineno: int) -> int:
            return ui_content.get_height_for_line(lineno, width, self.get_line_prefix)
        # topmost_visible: 贴底时最顶可见逻辑行(= vertical_scroll 上界)
        topmost = line_count - 1
        used = 0
        for lineno in range(line_count - 1, -1, -1):
            used += get_line_height(lineno)
            if used > height:
                break
            topmost = lineno
        ctl = self._scroll_ctl
        if ctl.following:
            self.vertical_scroll = topmost
            # 末行为超长行时,行内滚到行尾让最新输出可见(修"本次交互看不全")
            last_h = get_line_height(line_count - 1)
            self.vertical_scroll_2 = max(0, last_h - height) if last_h > height else 0
        else:
            clamped = max(0, min(ctl.scroll_offset, topmost))
            self.vertical_scroll = clamped
            self.vertical_scroll_2 = 0
            # 滚到底自动恢复跟随(后续新内容自动贴底)
            if clamped >= topmost:
                ctl.following = True
    def _scroll_up(self) -> None:
        """滚轮上:直接走 ScrollController,不走 cursor 逻辑"""
        self._scroll_ctl.scroll_up()
    def _scroll_down(self) -> None:
        """滚轮下:直接走 ScrollController"""
        self._scroll_ctl.scroll_down()
class ScrollController:
    """用 vertical_scroll 值(逻辑行)直接驱动滚动的控制器
    Args:
        get_fragments: 返回内容区 fragments 的回调(通常来自 ContentBuffer)
        invalidate:    触发 TUI 重绘的回调
    """
    # PageUp / PageDown 翻页步长(逻辑行)
    PAGE_STEP = 20
    # 鼠标滚轮步长(逻辑行)
    WHEEL_STEP = 3
    def __init__(
        self,
        get_fragments: Callable[[], StyleAndTextTuples],
        invalidate: Callable[[], None],
    ) -> None:
        self._get_fragments = get_fragments
        self._invalidate = invalidate
        # vertical_scroll 目标值(逻辑行);following=True 时忽略,由 _scroll 贴底
        self.scroll_offset: int = 0
        # 自动跟随末尾;用户上滚置 False,滚到底自动恢复 True
        self.following: bool = True
    # ── 提供给 FormattedTextControl 的回调 ──
    def get_content_fragments(self) -> StyleAndTextTuples:
        """内容区文本回调"""
        return self._get_fragments()
    # ── 滚动动作 ──
    def scroll_up(self, n: int = WHEEL_STEP) -> None:
        """滚轮向上:scroll_offset 减,暂停跟随"""
        self.following = False
        self.scroll_offset = max(0, self.scroll_offset - n)
        self._invalidate()
    def scroll_down(self, n: int = WHEEL_STEP) -> None:
        """滚轮向下:scroll_offset 增(到底由 _scroll 自动恢复跟随)"""
        self.scroll_offset += n
        self._invalidate()
    def page_up(self) -> None:
        """PageUp:向上翻 PAGE_STEP 行(暂停自动跟随)"""
        self.following = False
        self.scroll_offset = max(0, self.scroll_offset - self.PAGE_STEP)
        self._invalidate()
    def page_down(self) -> None:
        """PageDown:向下翻 PAGE_STEP 行(到达末尾即恢复自动跟随)"""
        self.scroll_offset += self.PAGE_STEP
        self._invalidate()
    def to_home(self) -> None:
        """Home:跳到内容顶部"""
        self.following = False
        self.scroll_offset = 0
        self._invalidate()
    def to_end(self) -> None:
        """End:跳到内容末尾,恢复自动跟随"""
        self.following = True
        self._invalidate()
    def scroll_to_end(self) -> None:
        """强制跟随末尾
        在用户输入后立即调用,确保 Agent 输出前滚动位置已在末尾。
        """
        self.following = True
        self._invalidate()
    def on_content_change(self) -> None:
        """ContentBuffer 变更回调 -> 触发重绘
        following 时 _scroll 会把 vertical_scroll 钉到新末尾(自动跟随);
        非 following 时保持 scroll_offset(用户在看历史)。
        """
        self._invalidate()