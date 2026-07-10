"""ChatEvent 事件类型测试"""

from harness_agent.chat.events import (
    EventType,
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    usage_event,
    thinking_event,
    retry_event,
    rate_limit_event,
    stream_error_event,
    cancelled_event,
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


# ── Retry / Rate Limit 事件测试 ──

def test_retry_event():
    """重试事件创建"""
    event = retry_event(attempt=2, reason="connection_failed")
    assert event.type == EventType.RETRY
    assert event.data["attempt"] == 2
    assert event.data["reason"] == "connection_failed"
    assert "max_attempts" not in event.data


def test_retry_event_with_max():
    """带最大次数的重试事件"""
    event = retry_event(attempt=3, reason="timeout", max_attempts=5)
    assert event.type == EventType.RETRY
    assert event.data["attempt"] == 3
    assert event.data["max_attempts"] == 5


def test_rate_limit_event():
    """速率限制事件创建"""
    event = rate_limit_event(
        status="allowed_warning",
        rate_limit_type="seven_day",
        utilization=0.85,
        message="接近限制",
    )
    assert event.type == EventType.RATE_LIMIT
    assert event.data["status"] == "allowed_warning"
    assert event.data["rate_limit_type"] == "seven_day"
    assert event.data["utilization"] == 0.85
    assert event.data["message"] == "接近限制"


def test_rate_limit_event_rejected():
    """被拒绝的速率限制事件"""
    event = rate_limit_event(
        status="rejected",
        rate_limit_type="five_hour",
        resets_at=1234567890,
        message="已达到限制",
    )
    assert event.type == EventType.RATE_LIMIT
    assert event.data["status"] == "rejected"
    assert event.data["resets_at"] == 1234567890


def test_stream_error_event():
    """流错误事件创建"""
    event = stream_error_event(
        error="API connection timeout",
        stream_event_type="error",
    )
    assert event.type == EventType.STREAM_ERROR
    assert event.data["error"] == "API connection timeout"
    assert event.data["stream_event_type"] == "error"


def test_cancelled_event():
    """取消事件创建"""
    event = cancelled_event("用户取消了请求")
    assert event.type == EventType.CANCELLED
    assert event.data["reason"] == "用户取消了请求"
