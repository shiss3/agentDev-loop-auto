"""ChatEvent 事件类型测试"""

from harness_agent.chat.events import (
    ChatEvent,
    EventType,
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    usage_event,
    thinking_event,
)


def test_text_event_creation():
    event = text_event("hello")
    assert event.type == EventType.TEXT
    assert event.data["text"] == "hello"
    assert event.agent == "default"


def test_text_event_custom_agent():
    event = text_event("hello", agent="frontend")
    assert event.agent == "frontend"


def test_tool_use_event_creation():
    event = tool_use_event("Read", "id-1", "file.py")
    assert event.type == EventType.TOOL_USE
    assert event.data["tool_name"] == "Read"
    assert event.data["tool_id"] == "id-1"
    assert event.data["tool_input"] == "file.py"


def test_tool_result_event_creation():
    event = tool_result_event("id-1", "file contents...", is_error=False)
    assert event.type == EventType.TOOL_RESULT
    assert event.data["tool_use_id"] == "id-1"
    assert event.data["is_error"] is False


def test_tool_result_event_error():
    event = tool_result_event("id-1", "error msg", is_error=True)
    assert event.data["is_error"] is True


def test_turn_lifecycle_events():
    start = turn_start_event(1, "test prompt")
    assert start.type == EventType.TURN_START
    assert start.data["turn_number"] == 1
    assert start.data["prompt"] == "test prompt"

    end = turn_end_event(1, tool_count=3, duration_ms=1500)
    assert end.type == EventType.TURN_END
    assert end.data["tool_count"] == 3
    assert end.data["duration_ms"] == 1500


def test_error_event():
    event = error_event("something broke")
    assert event.type == EventType.ERROR
    assert event.data["error"] == "something broke"


def test_usage_event():
    event = usage_event(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=80,
    )
    assert event.type == EventType.USAGE
    assert event.data["input_tokens"] == 100
    assert event.data["output_tokens"] == 50
    assert event.data["cache_read_tokens"] == 80
    assert event.data["cache_creation_tokens"] == 0


def test_thinking_event():
    event = thinking_event()
    assert event.type == EventType.THINKING


def test_event_type_is_string():
    """EventType 继承 str，可以作为字典 key"""
    assert isinstance(EventType.TEXT, str)
    assert EventType.TEXT == "text"
