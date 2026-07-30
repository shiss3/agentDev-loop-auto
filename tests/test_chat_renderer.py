"""ChatRenderer 渲染器测试（TUI 版）

验证 ChatRenderer 将 ChatEvent 正确转换为 ContentBuffer 操作。
不测试 prompt_toolkit Application（需要真实终端）。
"""

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.tui_app import TuiApp
from autoloop_agent.chat.renderer import ChatRenderer
from autoloop_agent.chat.events import (
    ChatEvent,
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    thinking_event,
    usage_event,
)


def _make_renderer() -> tuple[ChatRenderer, ContentBuffer, TuiApp]:
    """创建测试用 Renderer + ContentBuffer + TuiApp（不创建 Application）"""
    buf = ContentBuffer()
    tui = TuiApp(content_buffer=buf, model_name="test-model")
    renderer = ChatRenderer(tui=tui)
    return renderer, buf, tui


def _get_content(buf: ContentBuffer) -> str:
    """提取 ContentBuffer 的纯文本内容"""
    ft = buf.get_formatted_text()
    return "".join(t for _, t in ft)


# ── TEXT ──


def test_text_render():
    renderer, buf, _ = _make_renderer()
    renderer.handle(text_event("hello world"))
    assert "hello world" in _get_content(buf)


# ── TOOL_USE ──


def test_tool_render_increments_count():
    renderer, _, _ = _make_renderer()
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    assert renderer.total_tool_count == 1
    renderer.handle(tool_use_event("Write", "id-2", "out.py"))
    assert renderer.total_tool_count == 2


def test_tool_use_appears_in_content():
    renderer, buf, _ = _make_renderer()
    renderer.handle(turn_start_event(1, "test"))  # stop spinner first
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    content = _get_content(buf)
    assert "Read" in content
    assert "file.py" in content


# ── TOOL_RESULT ──


def test_tool_result_error():
    renderer, buf, _ = _make_renderer()
    renderer.handle(turn_start_event(1, "test"))
    renderer.handle(tool_result_event("id-1", "command failed", is_error=True))
    content = _get_content(buf)
    assert "失败" in content or "failed" in content


# ── ERROR ──


def test_error_render():
    renderer, buf, _ = _make_renderer()
    renderer.handle(error_event("something broke"))
    content = _get_content(buf)
    assert "something broke" in content


# ── TURN_START ──


def test_turn_start_shows_user_input():
    renderer, buf, _ = _make_renderer()
    renderer.handle(turn_start_event(1, "analyze user.py"))
    content = _get_content(buf)
    assert "You >" in content
    assert "analyze user.py" in content


def test_turn_start_resets_tool_count():
    renderer, _, _ = _make_renderer()
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    assert renderer.turn_tool_count == 1
    renderer.handle(turn_start_event(1, "test"))
    assert renderer.turn_tool_count == 0


# ── TURN_END ──


def test_turn_end_updates_status():
    renderer, buf, tui = _make_renderer()
    # Turn End 使用 renderer 内部的 turn_tool_count 统计
    renderer.handle(turn_start_event(1, "test"))
    renderer.handle(tool_use_event("Read", "id-1", "a.py"))
    renderer.handle(tool_use_event("Write", "id-2", "b.py"))
    renderer.handle(tool_use_event("Edit", "id-3", "c.py"))
    renderer.handle(turn_end_event(1, tool_count=0, duration_ms=2500))
    assert "3 次工具" in tui._status_text
    assert "2.5s" in tui._status_text


def test_turn_end_zero_stats():
    renderer, buf, tui = _make_renderer()
    renderer.handle(turn_end_event(1, tool_count=0, duration_ms=0))
    # 无工具和耗时时，状态栏只有模型名
    assert "test-model" in tui._status_text


# ── USAGE ──


def test_usage_accumulates():
    renderer, _, _ = _make_renderer()
    renderer.handle(usage_event(input_tokens=100, output_tokens=50))
    assert renderer._turn_input_tokens == 100
    assert renderer._turn_output_tokens == 50
    renderer.handle(usage_event(input_tokens=200, output_tokens=100))
    assert renderer._turn_input_tokens == 300
    assert renderer._turn_output_tokens == 150


# ── THINKING ──


def test_thinking_starts_spinner():
    renderer, buf, _ = _make_renderer()
    renderer.handle(thinking_event())
    assert buf.spinner_active is True


# ── WELCOME / GOODBYE ──


def test_render_welcome():
    renderer, buf, tui = _make_renderer()
    renderer.render_welcome("/my/project", "0.1.0")
    content = _get_content(buf)
    assert "0.1.0" in content
    assert "/my/project" in content
    assert "就绪" in tui._status_text


def test_render_goodbye():
    renderer, buf, _ = _make_renderer()
    renderer.render_goodbye({"turn_count": 5, "total_tool_calls": 12})
    content = _get_content(buf)
    assert "5" in content
    assert "12" in content


def test_render_command_result():
    renderer, buf, _ = _make_renderer()
    renderer.render_command_result("test output")
    content = _get_content(buf)
    assert "test output" in content


# ── FULL LIFECYCLE ──


def test_full_turn_lifecycle():
    """验证完整的 Turn 生命周期：start → text → tool → result → usage → end"""
    renderer, buf, tui = _make_renderer()

    renderer.handle(turn_start_event(1, "analyze user.py"))
    assert "You >" in _get_content(buf)
    assert buf.spinner_active is True

    renderer.handle(text_event("let me check..."))
    assert "let me check..." in _get_content(buf)

    renderer.handle(tool_use_event("Read", "id-1", "user.py"))
    assert "Read" in _get_content(buf)
    assert renderer.total_tool_count == 1

    renderer.handle(tool_result_event("id-1", "file content", is_error=False))

    renderer.handle(usage_event(input_tokens=500, output_tokens=200))

    renderer.handle(turn_end_event(1, tool_count=1, duration_ms=3000))

    # 验证最终状态
    assert buf.spinner_active is False
    # 500 < 1000 阈值，不转换为 k
    assert "in=500" in tui._status_text
    assert "1 次工具" in tui._status_text
    assert "3.0s" in tui._status_text


# ── UNKNOWN EVENT ──


def test_handle_unknown_event_type_is_noop():
    renderer, _, _ = _make_renderer()
    event = ChatEvent(type="nonexistent_type", data={})  # type: ignore
    renderer.handle(event)  # 不应抛异常


# ── MULTIPLE TURNS ──


def test_tool_count_accumulates_across_turns():
    renderer, _, _ = _make_renderer()

    # Turn 1
    renderer.handle(turn_start_event(1, "task 1"))
    renderer.handle(tool_use_event("Read", "id-1", "a.py"))
    renderer.handle(turn_end_event(1, tool_count=1, duration_ms=1000))
    assert renderer.total_tool_count == 1

    # Turn 2
    renderer.handle(turn_start_event(2, "task 2"))
    renderer.handle(tool_use_event("Write", "id-2", "b.py"))
    renderer.handle(tool_use_event("Edit", "id-3", "c.py"))
    renderer.handle(turn_end_event(2, tool_count=2, duration_ms=2000))
    assert renderer.total_tool_count == 3
