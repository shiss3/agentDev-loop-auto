"""BaseAgentSession 底座专属测试 — Phase 2 Step 1 §8.3

测 BaseAgentSession（非 ChatSession）的三个新参数接线 + _resolve_system_prompt 钩子 +
AsyncIterable prompt + cost 累计。mock ClaudeSDKClient，不拉真实 CLI 子进程。
参考 test_session_send.py 的 mock 套路。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import ResultMessage

from harness_agent.core.base_session import BaseAgentSession


# ── 辅助 ──────────────────────────────────────────────


def _async_iter(messages):
    """把消息列表包成 async iterator（每次调用产出全新生成器）"""

    async def gen():
        for m in messages:
            yield m

    return gen()


def _make_mock_client():
    """构造 mock ClaudeSDKClient（参考 test_session_send.py）。"""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.query = AsyncMock(return_value=None)
    client.interrupt = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    client.connect = AsyncMock(return_value=None)
    client.receive_response = MagicMock()
    return client


# ── 1. 默认 permission_mode ──


def test_default_permission_mode_accept_edits():
    """BaseAgentSession() 默认 permission_mode == 'acceptEdits'（ChatSession 旧行为）"""
    session = BaseAgentSession()
    opts = session._build_options()
    assert opts.permission_mode == "acceptEdits"


# ── 2. permission_mode 可配 ──


def test_permission_mode_configurable():
    """传 permission_mode='default' → opts.permission_mode == 'default'"""
    session = BaseAgentSession(permission_mode="default")
    opts = session._build_options()
    assert opts.permission_mode == "default"


# ── 3. can_use_tool 接线 ──


def test_can_use_tool_propagated():
    """传 can_use_tool=cb → opts.can_use_tool is cb；不传 → 无该属性（或 None）"""
    def cb(*args, **kwargs):
        return True

    session_with = BaseAgentSession(can_use_tool=cb)
    opts_with = session_with._build_options()
    assert opts_with.can_use_tool is cb

    session_without = BaseAgentSession()
    opts_without = session_without._build_options()
    assert getattr(opts_without, "can_use_tool", None) is None


# ── 4. _resolve_system_prompt 默认 ──


def test_resolve_system_prompt_default():
    """BaseAgentSession(system_prompt='X')._resolve_system_prompt() == 'X'"""
    session = BaseAgentSession(system_prompt="X")
    assert session._resolve_system_prompt() == "X"


# ── 5. _resolve_system_prompt 子类可覆盖 ──


def test_resolve_system_prompt_subclass_override():
    """子类覆盖 _resolve_system_prompt → 返回自定义值（验证钩子可扩展）"""

    class CustomSession(BaseAgentSession):
        def _resolve_system_prompt(self):
            return {"type": "preset", "preset": "claude_code"}

    session = CustomSession()
    assert session._resolve_system_prompt() == {"type": "preset", "preset": "claude_code"}


# ── 6. send() 用 AsyncIterable prompt（非字符串）──


async def test_send_uses_asynciterable_prompt():
    """send('hi') → client.query 收到的是 async generator，迭代出标准 user 消息 dict"""
    with patch("harness_agent.core.base_session.ClaudeSDKClient") as MockClient:
        client = _make_mock_client()
        MockClient.return_value = client
        session = BaseAgentSession()
        await session.start()
        try:
            client.receive_response = MagicMock(return_value=_async_iter([]))
            # 消费事件流（空响应 → TURN_START + TURN_END）
            _ = [e async for e in session.send("hi")]

            # query 被调用一次
            assert client.query.await_count == 1
            arg = client.query.call_args.args[0]

            # 不是字符串
            assert not isinstance(arg, str)
            # 是 async generator
            assert hasattr(arg, "__anext__")

            # 迭代它，yield 出标准 user 消息
            items = []
            async for item in arg:
                items.append(item)
            assert len(items) == 1
            assert items[0] == {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "parent_tool_use_id": None,
            }
            # 回归：不得硬编码 session_id —— SDK query() 会按 session_id 参数
            # 填充（默认 "default"）；硬编码 "" 会让 CLI 每轮当新会话，丢失多轮记忆
            assert "session_id" not in items[0]
        finally:
            if session.is_active:
                await session.close()


# ── 7. total_cost_usd 累计 ──


async def test_total_cost_usd_accumulated():
    """ResultMessage(total_cost_usd=0.5) → session.stats.total_cost_usd == 0.5"""
    with patch("harness_agent.core.base_session.ClaudeSDKClient") as MockClient:
        client = _make_mock_client()
        MockClient.return_value = client
        session = BaseAgentSession()
        await session.start()
        try:
            rm = ResultMessage(
                subtype="result",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id="s1",
                total_cost_usd=0.5,
            )
            client.receive_response = MagicMock(return_value=_async_iter([rm]))

            _ = [e async for e in session.send("hi")]

            assert session.stats.total_cost_usd == 0.5
        finally:
            if session.is_active:
                await session.close()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
