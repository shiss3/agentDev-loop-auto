"""ChatSession.send() 回归测试 — 重构前的行为基准

锁定 ChatSession (src/autoloop_agent/chat/session.py) 的当前行为，
作为 Phase 2 Step 1（抽离 BaseAgentSession + send() 改流式）重构的回归网。

测试策略：
- Mock ClaudeSDKClient（不拉真实 CLI 子进程）
- 用真实 SDK 消息类型 + 真实 MessageTranslator（translator 行为同时被覆盖）
- 覆盖 send()/cancel()/retry/rate_limit/error 关键路径

⚠️ 当前行为基线发现（非测试 bug，是 session.py 现状）：
- translator 从 ResultMessage 只提取 USAGE / ERROR，从不产出 TOOL_RESULT 事件。
  session.py 导入了 tool_result_event 但全文件未调用 —— TOOL_RESULT 是死路径。
  本测试锁定该现状，重构后应保持一致或显式改变。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from autoloop_agent.chat.events import EventType
from autoloop_agent.chat.session import ChatSession


# ── 辅助 ──────────────────────────────────────────────


def _async_iter(messages):
    """把消息列表包成 async iterator（每次调用产出全新生成器）"""

    async def gen():
        for m in messages:
            yield m

    return gen()


def _make_mock_client():
    """构造 mock ClaudeSDKClient。

    receive_response 留给各测试单独配置（它是 sync 调用返回 async iterator，
    不是 async 方法，故用 MagicMock 而非 AsyncMock）。
    """
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.query = AsyncMock(return_value=None)
    client.interrupt = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    client.connect = AsyncMock(return_value=None)
    client.receive_response = MagicMock()
    return client


@pytest.fixture
async def started_session():
    """已 start() 的 ChatSession + 其 mock client。

    ClaudeSDKClient 被 patch，不拉真实 CLI 子进程。
    """
    with patch("autoloop_agent.core.base_session.ClaudeSDKClient") as MockClient:
        client = _make_mock_client()
        MockClient.return_value = client
        session = ChatSession(project_dir=".")
        await session.start()
        yield session, client
        if session.is_active:
            await session.close()


def _types(events):
    return [e.type for e in events]


# ── 1. 未启动 send ────────────────────────────────────


async def test_send_before_start_yields_error():
    """send() 在 start() 前调用 → 产出 error_event，不递增 turn_count"""
    session = ChatSession(project_dir=".")
    events = [e async for e in session.send("hi")]

    assert len(events) == 1
    assert events[0].type == EventType.ERROR
    assert "会话未启动" in events[0].data["error"]
    # 未启动路径在 turn_count+=1 之前 return
    assert session.stats.turn_count == 0


# ── 2. 文本流 ────────────────────────────────────────


async def test_text_stream(started_session):
    """AssistantMessage(content=[TextBlock]) → TEXT 事件"""
    session, client = started_session
    msg = AssistantMessage(
        content=[TextBlock(text="hello world")], model="claude-sonnet-4"
    )
    client.receive_response = MagicMock(return_value=_async_iter([msg]))

    events = [e async for e in session.send("hi")]

    assert EventType.TEXT in _types(events)
    text_ev = next(e for e in events if e.type == EventType.TEXT)
    assert text_ev.data["text"] == "hello world"


# ── 3. 工具调用流 ────────────────────────────────────


async def test_tool_use_flow(started_session):
    """AssistantMessage(ToolUseBlock) → TOOL_USE；ResultMessage → USAGE（当前行为）"""
    session, client = started_session
    tool = ToolUseBlock(id="tu1", name="Read", input={"file_path": "test.txt"})
    msg = AssistantMessage(content=[tool], model="claude-sonnet-4")
    # ResultMessage 携带 usage —— translator 当前从此提取 USAGE 而非 TOOL_RESULT
    result = ResultMessage(
        subtype="result",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="s1",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    client.receive_response = MagicMock(return_value=_async_iter([msg, result]))

    events = [e async for e in session.send("read the file")]

    types = _types(events)
    # TOOL_USE 事件 + 字段
    assert EventType.TOOL_USE in types
    tu = next(e for e in events if e.type == EventType.TOOL_USE)
    assert tu.data["tool_name"] == "Read"
    assert tu.data["tool_id"] == "tu1"
    # summarize_tool_input 对含 file_path 的 input 返回路径本身
    assert tu.data["tool_input"] == "test.txt"
    # 当前行为：ResultMessage 产出 USAGE，不产出 TOOL_RESULT
    assert EventType.TOOL_RESULT not in types
    assert EventType.USAGE in types
    # 工具调用计入本轮统计
    assert EventType.TURN_END in types
    end_ev = next(e for e in events if e.type == EventType.TURN_END)
    assert end_ev.data["tool_count"] == 1


# ── 4. TURN_START / TURN_END ─────────────────────────


async def test_turn_lifecycle_events(started_session):
    """send() 首事件 TURN_START(turn_number=1, prompt)；末事件 TURN_END"""
    session, client = started_session
    msg = AssistantMessage(
        content=[TextBlock(text="response")], model="claude-sonnet-4"
    )
    client.receive_response = MagicMock(return_value=_async_iter([msg]))

    events = [e async for e in session.send("test prompt")]

    # 首事件
    assert events[0].type == EventType.TURN_START
    assert events[0].data["turn_number"] == 1
    assert events[0].data["prompt"] == "test prompt"
    # 末事件
    assert events[-1].type == EventType.TURN_END
    assert events[-1].data["turn_number"] == 1
    assert events[-1].data["tool_count"] == 0
    assert events[-1].data["duration_ms"] >= 0


# ── 5. USAGE / token 累计 ────────────────────────────


async def test_usage_and_token_accumulation(started_session):
    """ResultMessage 携带 usage → stats 累加 + actual_model 更新"""
    session, client = started_session
    am = AssistantMessage(content=[TextBlock(text="hi")], model="claude-sonnet-4")
    rm = ResultMessage(
        subtype="result",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=1,
        session_id="s1",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
        },
        model_usage={"claude-sonnet-4": {}},
    )
    client.receive_response = MagicMock(return_value=_async_iter([am, rm]))

    events = [e async for e in session.send("hi")]

    assert EventType.USAGE in _types(events)
    usage_ev = next(e for e in events if e.type == EventType.USAGE)
    assert usage_ev.data["input_tokens"] == 100
    assert usage_ev.data["output_tokens"] == 50
    assert usage_ev.data["cache_read_tokens"] == 10
    assert usage_ev.data["cache_creation_tokens"] == 5
    assert usage_ev.data["model_name"] == "claude-sonnet-4"
    # token 累计
    assert session.stats.total_input_tokens == 100
    assert session.stats.total_output_tokens == 50
    # actual_model 更新（AssistantMessage.model + ResultMessage.model_usage 均会更新）
    assert session.actual_model == "claude-sonnet-4"


# ── 6. stats 跨轮累计 ────────────────────────────────


async def test_stats_accumulate_across_turns(started_session):
    """连续两次 send() → turn_count=2, total_tool_calls 累加"""
    session, client = started_session
    msg1 = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "a.txt"})],
        model="claude-sonnet-4",
    )
    msg2 = AssistantMessage(
        content=[ToolUseBlock(id="t2", name="Bash", input={"command": "ls"})],
        model="claude-sonnet-4",
    )
    # 每次 receive_response() 调用返回全新生成器（生成器单次使用）
    client.receive_response = MagicMock(
        side_effect=[_async_iter([msg1]), _async_iter([msg2])]
    )

    _ = [e async for e in session.send("turn 1")]
    events2 = [e async for e in session.send("turn 2")]

    assert session.stats.turn_count == 2
    assert session.stats.total_tool_calls == 2
    # 第二轮 TURN_START 的 turn_number 递增到 2
    assert events2[0].type == EventType.TURN_START
    assert events2[0].data["turn_number"] == 2


# ── 7. cancel ────────────────────────────────────────


async def test_cancel_during_stream(started_session):
    """send() 循环顶部检测 _cancelled → interrupt + cancelled_event + 恢复客户端

    时序：receive_response 在 yield 两条消息之间让测试设 _cancelled=True，
    下一次迭代顶部检测到 → 触发中断。TURN_END 在 L567 无条件产出。
    """
    session, client = started_session
    # msg1 含 1 个工具调用 —— 让 total_tool_calls==0 断言有意义：
    # 若 was_cancelled 跳过 stats 更新块（L547-550）被破坏，total_tool_calls 会变成 1
    msg1 = AssistantMessage(
        content=[
            TextBlock(text="partial"),
            ToolUseBlock(id="tu1", name="Read", input={"file_path": "x.txt"}),
        ],
        model="claude-sonnet-4",
    )
    msg2 = AssistantMessage(
        content=[TextBlock(text="should-not-appear")], model="claude-sonnet-4"
    )

    async def gen():
        yield msg1
        # 在两条消息之间触发取消（模拟用户按 ESC）
        session._cancelled = True
        yield msg2  # 循环顶部检测到 _cancelled → 中断，msg2 不会被翻译

    client.receive_response = MagicMock(return_value=gen())

    events = [e async for e in session.send("do something")]

    types = _types(events)
    # 取消事件
    assert EventType.CANCELLED in types
    cancel_ev = next(e for e in events if e.type == EventType.CANCELLED)
    assert "取消" in cancel_ev.data["reason"]
    # 被取消后不再产出 msg2 的文本
    texts = [e.data["text"] for e in events if e.type == EventType.TEXT]
    assert "should-not-appear" not in texts
    # TURN_END 仍产出（L567 无条件）
    assert events[-1].type == EventType.TURN_END
    # interrupt 被调用
    assert client.interrupt.await_count == 1
    # 恢复客户端：disconnect + connect
    assert client.disconnect.await_count == 1
    assert client.connect.await_count == 1
    # 统计
    assert session.stats.cancelled_requests == 1
    # 取消时跳过 stats 更新块（L547-550）：msg1 含 1 个工具调用（turn_tool_count=1），
    # 若跳过逻辑被破坏 total_tool_calls 会变成 1，断言==0 才有意义
    assert session.stats.total_tool_calls == 0
    # turn_count 在 send 开头就 +1，不回滚
    assert session.stats.turn_count == 1


# ── 8. retry 检测 ────────────────────────────────────


async def test_retry_detection_during_stream(started_session):
    """stderr 回调在 receive_response 循环期间触发 → retry_event

    关键：send() 开头 L466 把 _retry_count 重置为 0，所以 stderr 回调
    必须在循环期间触发才有 retry_event。在 yield 前调 _handle_stderr_line。
    """
    session, client = started_session
    msg = AssistantMessage(content=[TextBlock(text="done")], model="claude-sonnet-4")

    async def gen():
        # 在 yield 前触发 stderr 重试日志（send 开头已重置 _retry_count=0）。
        # ⚠️ 现状锁定：session.py 的 _RETRY_PATTERNS 正则 retrying.*?\((\d+)/(\d+)\)
        # 要求 '(' 紧跟数字，因此 "Retrying... (attempt 2/5)" 反而**不匹配**
        # （注释声称匹配但实际不行 —— session.py bug，此处不改被测代码）。
        # 用确实能匹配的 "Retrying request (2/5)" 锁定 retry 检测路径。
        session._handle_stderr_line("Retrying request (2/5)")
        yield msg

    client.receive_response = MagicMock(return_value=gen())

    events = [e async for e in session.send("hi")]

    types = _types(events)
    assert EventType.RETRY in types
    retry_ev = next(e for e in events if e.type == EventType.RETRY)
    assert retry_ev.data["attempt"] == 2
    assert retry_ev.data["reason"] == "retry_attempt"
    # stats.retry_count 取最大值
    assert session.stats.retry_count == 2
    assert session.stats.last_retry_reason == "retry_attempt"


# ── 9. rate_limit ────────────────────────────────────


async def test_rate_limit_event(started_session):
    """RateLimitEvent(rejected) → RATE_LIMIT 事件 + rate_limit_hits 累加"""
    session, client = started_session
    rli = RateLimitInfo(
        status="rejected",
        resets_at=1234567890,
        rate_limit_type="five_hour",
        utilization=0.9,
    )
    msg = RateLimitEvent(rate_limit_info=rli, uuid="u1", session_id="s1")
    client.receive_response = MagicMock(return_value=_async_iter([msg]))

    events = [e async for e in session.send("hi")]

    types = _types(events)
    assert EventType.RATE_LIMIT in types
    rl_ev = next(e for e in events if e.type == EventType.RATE_LIMIT)
    assert rl_ev.data["status"] == "rejected"
    assert rl_ev.data["rate_limit_type"] == "five_hour"
    assert rl_ev.data["resets_at"] == 1234567890
    assert rl_ev.data["utilization"] == 0.9
    # 统计
    assert session.stats.last_rate_limit_status == "rejected"
    assert session.stats.rate_limit_hits == 1


# ── 10. error ────────────────────────────────────────


async def test_send_swallows_sdk_exception(started_session):
    """receive_response 抛异常 → error_event("SDK 异常: ...")，不崩溃"""
    session, client = started_session
    client.receive_response = MagicMock(side_effect=RuntimeError("boom"))

    events = [e async for e in session.send("hi")]

    types = _types(events)
    assert EventType.ERROR in types
    err_ev = next(e for e in events if e.type == EventType.ERROR)
    assert "SDK 异常" in err_ev.data["error"]
    assert "boom" in err_ev.data["error"]
    # 不崩溃，TURN_END 仍产出
    assert events[-1].type == EventType.TURN_END
    # 异常路径仍递增 turn_count（send 开头就 +1）
    assert session.stats.turn_count == 1


# ── 10b. ResultMessage is_error（translator L182-186） ──


async def test_result_message_is_error_yields_error_event(started_session):
    """ResultMessage(is_error=True, errors=[...]) → error_event

    覆盖 translator._translate_result 的 is_error 分支（L182-186），
    该分支从 message.errors 拼接错误文本。区别于路径 10 的 Python 异常分支。
    """
    session, client = started_session
    rm = ResultMessage(
        subtype="result",
        duration_ms=10,
        duration_api_ms=5,
        is_error=True,
        num_turns=1,
        session_id="s1",
        errors=["something went wrong", "code=500"],
    )
    client.receive_response = MagicMock(return_value=_async_iter([rm]))

    events = [e async for e in session.send("hi")]

    types = _types(events)
    assert EventType.ERROR in types
    err_ev = next(e for e in events if e.type == EventType.ERROR)
    # translator 把 errors 用 \n join 后截断 500 字符
    assert "something went wrong" in err_ev.data["error"]
    assert "code=500" in err_ev.data["error"]
    # 不崩溃，TURN_END 仍产出
    assert events[-1].type == EventType.TURN_END


# ── 8b. retry 计数跨轮重置（session.py L466） ──


async def test_retry_count_reset_between_turns(started_session):
    """send() 开头 L466 把 _retry_count 重置为 0 —— 上一轮的 retry 不污染下一轮

    若 L466 重置被删除：第二轮带着上一轮的 _retry_count=2 进入循环，
    2 > last_emitted(0) → 产出虚假 RETRY 事件。本测试锁定重置行为。
    """
    session, client = started_session
    msg = AssistantMessage(content=[TextBlock(text="ok")], model="claude-sonnet-4")

    async def gen1():
        # 第一轮触发 retry（attempt 2）
        session._handle_stderr_line("Retrying request (2/5)")
        yield msg

    async def gen2():
        # 第二轮不触发任何 stderr
        yield msg

    client.receive_response = MagicMock(side_effect=[gen1(), gen2()])

    events1 = [e async for e in session.send("turn 1")]
    events2 = [e async for e in session.send("turn 2")]

    # 第一轮有 RETRY（attempt 2）
    assert EventType.RETRY in _types(events1)
    # 第二轮不应有 RETRY —— _retry_count 在 send 开头被重置为 0
    assert EventType.RETRY not in _types(events2)
    # stats.retry_count 仍保留历史最大值（不被 send 重置）
    assert session.stats.retry_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
