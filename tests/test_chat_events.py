"""ChatEvent 事件类型测试"""

from datetime import datetime

from autoloop_agent.chat.events import (
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


# ── 序列化 / 反序列化往返测试 ──

def _assert_roundtrip(event):
    d = event.to_dict()
    # 协议字段齐全
    assert set(d.keys()) == {"type", "data", "agent", "request_id", "timestamp"}
    # type 走 str value，非 Enum
    assert d["type"] == event.type.value
    assert isinstance(d["type"], str)
    # timestamp isoformat
    assert d["timestamp"] == event.timestamp.isoformat()
    # data 直传
    assert d["data"] == event.data
    # 往返对称
    restored = type(event).from_dict(d) if False else event.from_dict(d)
    assert restored.type == event.type
    assert restored.data == event.data
    assert restored.agent == event.agent
    assert restored.request_id == event.request_id
    assert restored.timestamp == event.timestamp


def test_roundtrip_text():
    _assert_roundtrip(text_event("hi", agent="a", request_id="r1"))


def test_roundtrip_tool_use():
    _assert_roundtrip(tool_use_event("Read", "id-1", "f.py", request_id="r1"))


def test_roundtrip_tool_result():
    _assert_roundtrip(tool_result_event("id-1", "content", is_error=True, request_id="r1"))


def test_roundtrip_usage():
    _assert_roundtrip(
        usage_event(input_tokens=10, output_tokens=5, model_name="m", request_id="r1")
    )


def test_roundtrip_retry():
    _assert_roundtrip(retry_event(attempt=2, reason="timeout", max_attempts=5, request_id="r1"))


def test_roundtrip_rate_limit():
    _assert_roundtrip(
        rate_limit_event(status="rejected", rate_limit_type="five_hour", resets_at=123, request_id="r1")
    )


def test_roundtrip_stream_error():
    _assert_roundtrip(stream_error_event(error="boom", stream_event_type="error", request_id="r1"))


def test_roundtrip_turn_lifecycle():
    _assert_roundtrip(turn_start_event(1, "prompt", request_id="r1"))
    _assert_roundtrip(turn_end_event(1, tool_count=3, duration_ms=1500, request_id="r1"))


def test_roundtrip_misc():
    _assert_roundtrip(error_event("err", agent="a", request_id="r1"))
    _assert_roundtrip(thinking_event(agent="a", request_id="r1"))
    _assert_roundtrip(cancelled_event(reason="取消", request_id="r1"))


def test_roundtrip_covers_all_event_types():
    """每个 EventType 至少有一个工厂产出，并完成往返。"""
    covered: set[EventType] = set()
    events = [
        text_event("x"),
        tool_use_event("t", "id", "in"),
        tool_result_event("id", "out"),
        turn_start_event(1, "p"),
        turn_end_event(1, 0, 0),
        cancelled_event(),
        thinking_event(),
        error_event("e"),
        usage_event(),
        retry_event(1, "r"),
        rate_limit_event(status="allowed"),
        stream_error_event("e"),
    ]
    for ev in events:
        _assert_roundtrip(ev)
        covered.add(ev.type)
    missing = set(EventType) - covered
    assert not missing, f"未覆盖 EventType: {missing}"


def test_timestamp_isoformat_roundtrip():
    """显式时间戳经 to_dict/from_dict 不丢精度。"""
    ts = datetime(2026, 7, 26, 12, 34, 56, 789000)
    ev = ChatEvent(type=EventType.TEXT, data={"x": 1}, agent="a", timestamp=ts, request_id="r")
    d = ev.to_dict()
    assert d["timestamp"] == "2026-07-26T12:34:56.789000"
    assert ChatEvent.from_dict(d).timestamp == ts

