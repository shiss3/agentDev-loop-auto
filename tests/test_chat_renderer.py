"""ChatRenderer 渲染器测试"""

from io import StringIO

from rich.console import Console

from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.events import (
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    thinking_event,
)


def _make_renderer() -> tuple[ChatRenderer, StringIO]:
    """创建带 StringIO 捕获的测试 renderer"""
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=80)
    renderer = ChatRenderer(console=console)
    return renderer, buffer


def test_text_render():
    renderer, buf = _make_renderer()
    renderer.handle(text_event("hello world"))
    output = buf.getvalue()
    assert "hello world" in output


def test_tool_render_increments_count():
    renderer, buf = _make_renderer()
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    assert renderer.total_tool_count == 1
    renderer.handle(tool_use_event("Write", "id-2", "out.py"))
    assert renderer.total_tool_count == 2


def test_tool_result_render_success():
    renderer, buf = _make_renderer()
    renderer.handle(tool_result_event("id-1", "done", is_error=False))
    output = buf.getvalue()
    assert "✓" in output


def test_tool_result_render_error():
    renderer, buf = _make_renderer()
    renderer.handle(tool_result_event("id-1", "failed", is_error=True))
    output = buf.getvalue()
    assert "✗" in output


def test_error_render():
    renderer, buf = _make_renderer()
    renderer.handle(error_event("something broke"))
    output = buf.getvalue()
    assert "something broke" in output


def test_turn_end_shows_stats():
    renderer, buf = _make_renderer()
    renderer.handle(turn_end_event(1, tool_count=3, duration_ms=2500))
    output = buf.getvalue()
    # Rich ANSI 格式化会拆分数值，验证关键内容存在
    assert "3" in output
    assert "工具调用" in output
    assert "2500" in output or "2.5" in output or "2." in output


def test_turn_end_no_stats_when_zero():
    renderer, buf = _make_renderer()
    renderer.handle(turn_end_event(1, tool_count=0, duration_ms=0))
    output = buf.getvalue()
    # 只有换行，没有统计行
    assert "工具调用" not in output


def test_turn_start_resets_tool_count():
    renderer, buf = _make_renderer()
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    assert renderer.turn_tool_count == 1  # tool_use increments turn_tool_count
    renderer.handle(turn_start_event(1, "test"))
    assert renderer.turn_tool_count == 0  # turn_start resets it


def test_handle_unknown_event_type_is_noop():
    """传入 None handler 时不应报错"""
    renderer, _ = _make_renderer()
    # 创建一个没有 handler 的事件 — 不会 crash
    from harness_agent.chat.events import ChatEvent, EventType

    event = ChatEvent(type="nonexistent_type", data={})  # type: ignore
    renderer.handle(event)  # 不应抛异常
