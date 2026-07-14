"""ScrollController + _ScrollableWindow 单元测试
验证 vertical_scroll 直接驱动机制(方案 B):
- ScrollController 状态机(following / scroll_offset)
- _ScrollableWindow._scroll 的 clamp / 贴底 / 长行行内滚动 / 滚到底恢复跟随
"""
from dataclasses import dataclass, field
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import Dimension, FormattedTextControl
from harness_agent.chat.scroll_controller import ScrollController, _ScrollableWindow


def _make_ctl(fragments=None, invalidate=None) -> ScrollController:
    return ScrollController(
        get_fragments=fragments or (lambda: []),
        invalidate=invalidate or (lambda: None),
    )


@dataclass
class _MockUIContent:
    """模拟 prompt_toolkit UIContent,供 _scroll 测试"""
    line_count: int
    line_heights: dict
    cursor_position: Point = field(default_factory=lambda: Point(x=0, y=0))

    def get_height_for_line(self, lineno, width, get_line_prefix, slice_stop=None):
        return self.line_heights.get(lineno, 1)


def _make_window(ctl: ScrollController) -> _ScrollableWindow:
    ctrl = FormattedTextControl(text=lambda: "", focusable=False)
    return _ScrollableWindow(
        content=ctrl,
        wrap_lines=True,
        height=Dimension(min=3),
        scroll_ctl=ctl,
    )


# ── ScrollController 状态机 ──
class TestScrollControllerState:
    def test_initial_state(self):
        ctl = _make_ctl()
        assert ctl.following is True
        assert ctl.scroll_offset == 0

    def test_page_up_disables_following_and_decrements(self):
        ctl = _make_ctl()
        ctl.scroll_offset = 50
        ctl.page_up()
        assert ctl.following is False
        assert ctl.scroll_offset == 30  # 50 - 20

    def test_page_up_clamps_to_zero(self):
        ctl = _make_ctl()
        ctl.page_up()
        assert ctl.scroll_offset == 0
        assert ctl.following is False

    def test_page_down_increments(self):
        ctl = _make_ctl()
        ctl.page_down()
        assert ctl.scroll_offset == 20

    def test_to_home(self):
        ctl = _make_ctl()
        ctl.scroll_offset = 50
        ctl.to_home()
        assert ctl.scroll_offset == 0
        assert ctl.following is False

    def test_to_end_enables_following(self):
        ctl = _make_ctl()
        ctl.following = False
        ctl.to_end()
        assert ctl.following is True

    def test_scroll_to_end_enables_following(self):
        ctl = _make_ctl()
        ctl.following = False
        ctl.scroll_to_end()
        assert ctl.following is True

    def test_scroll_up_disables_following(self):
        ctl = _make_ctl()
        ctl.scroll_offset = 10
        ctl.scroll_up()
        assert ctl.following is False
        assert ctl.scroll_offset == 7  # 10 - 3

    def test_scroll_down_increments(self):
        ctl = _make_ctl()
        ctl.scroll_offset = 5
        ctl.scroll_down()
        assert ctl.scroll_offset == 8

    def test_on_content_change_preserves_state(self):
        ctl = _make_ctl()
        ctl.scroll_offset = 42
        ctl.following = False
        ctl.on_content_change()
        # 仅触发重绘,不改状态
        assert ctl.scroll_offset == 42
        assert ctl.following is False

    def test_invalidate_called_on_every_action(self):
        calls = []
        ctl = ScrollController(
            get_fragments=lambda: [], invalidate=lambda: calls.append(1)
        )
        ctl.page_up()
        ctl.page_down()
        ctl.to_home()
        ctl.to_end()
        ctl.scroll_up()
        ctl.scroll_down()
        ctl.on_content_change()
        assert len(calls) == 7


# ── _ScrollableWindow._scroll ──
class TestScrollableWindowScroll:
    def test_following_pins_to_topmost(self):
        ctl = _make_ctl()
        ctl.following = True
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=10, line_heights={i: 1 for i in range(10)})
        w._scroll(ui, width=80, height=3)
        # topmost: 行9/8/7 used=3 (<=3), 行6 used=4>3 break -> topmost=7
        assert w.vertical_scroll == 7
        assert w.vertical_scroll_2 == 0

    def test_non_following_uses_scroll_offset(self):
        ctl = _make_ctl()
        ctl.following = False
        ctl.scroll_offset = 5
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=10, line_heights={i: 1 for i in range(10)})
        w._scroll(ui, width=80, height=3)
        assert w.vertical_scroll == 5  # min(5, topmost=7)
        assert ctl.following is False  # 5 < 7, 未到底

    def test_scroll_offset_clamped_to_topmost(self):
        ctl = _make_ctl()
        ctl.following = False
        ctl.scroll_offset = 100  # 远超 topmost
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=10, line_heights={i: 1 for i in range(10)})
        w._scroll(ui, width=80, height=3)
        assert w.vertical_scroll == 7  # clamp 到 topmost

    def test_scroll_to_bottom_restores_following(self):
        ctl = _make_ctl()
        ctl.following = False
        ctl.scroll_offset = 100  # clamp 到 topmost=7 -> 7>=7 恢复
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=10, line_heights={i: 1 for i in range(10)})
        w._scroll(ui, width=80, height=3)
        assert ctl.following is True

    def test_long_last_line_vertical_scroll_2(self):
        ctl = _make_ctl()
        ctl.following = True
        w = _make_window(ctl)
        # 5 行,末行(4)高 10,其余高 1
        heights = {0: 1, 1: 1, 2: 1, 3: 1, 4: 10}
        ui = _MockUIContent(line_count=5, line_heights=heights)
        w._scroll(ui, width=80, height=3)
        # following: vertical_scroll=topmost, 末行 vertical_scroll_2 = 10-3 = 7
        assert w.vertical_scroll_2 == 7

    def test_content_shorter_than_window_topmost_zero(self):
        ctl = _make_ctl()
        ctl.following = True
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=2, line_heights={0: 1, 1: 1})
        w._scroll(ui, width=80, height=3)
        # 2 行 < 3 高,topmost=0
        assert w.vertical_scroll == 0

    def test_zero_width_resets_scroll(self):
        ctl = _make_ctl()
        ctl.following = True
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=10, line_heights={i: 1 for i in range(10)})
        w._scroll(ui, width=0, height=3)
        assert w.vertical_scroll == 0
        assert w.vertical_scroll_2 == 0

    def test_empty_content(self):
        ctl = _make_ctl()
        ctl.following = True
        w = _make_window(ctl)
        ui = _MockUIContent(line_count=0, line_heights={})
        w._scroll(ui, width=80, height=3)
        assert w.vertical_scroll == 0