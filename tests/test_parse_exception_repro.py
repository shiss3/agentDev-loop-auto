"""复现:auto 模式 _parse_requirement 抛异常 -> 应回注左栏常驻轨执行。"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import SessionStore

from autoloop_agent.chat.repl import ChatCLI


@pytest.mark.asyncio
async def test_parse_exception_injects_interactive():
    cli = ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))
    g = cli.governor  # 真 Governor

    async def boom(text, *, context_continuation):
        raise RuntimeError("需求解析未返回结构化结果")

    g._parse_requirement = boom

    sent = []

    class FakeResident:
        class stats:
            turn_count = 0

        async def send(self, text):
            sent.append(text)
            if False:
                yield

    g._resident = FakeResident()

    await cli._on_user_input("做个登录页")
    if cli._delivery_runner is not None:
        await cli._delivery_runner
    await asyncio.sleep(0)
    if cli._message_task is not None:
        await cli._message_task

    print("drawer states:", [(i.kind, i.state, i.detail) for i in cli._drawer.items])
    print("drawer logs:", cli._drawer.logs if hasattr(cli._drawer, "logs") else "?")
    print("last_track:", g.last_track)
    print("resident sent:", sent)
    assert sent == ["做个登录页"], "解析异常后未回注常驻轨执行"


@pytest.mark.asyncio
async def test_no_module_degradation_injects_left():
    """解析成功但无模块 -> 交付段降级置回 interactive -> 回注左栏常驻轨执行。"""
    cli = ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))
    g = cli.governor

    async def fake_parse(text, *, context_continuation):
        return {"task_summary": text, "risk_level": "low", "modules": []}

    g._parse_requirement = fake_parse

    sent = []

    class FakeResident:
        class stats:
            turn_count = 0

        async def send(self, text):
            sent.append(text)
            yield text_event("常驻输出")

    from autoloop_agent.chat.events import text_event
    g._resident = FakeResident()

    await cli._on_user_input("随便改点东西")
    if cli._delivery_runner is not None:
        await cli._delivery_runner
    await asyncio.sleep(0)
    if cli._message_task is not None:
        await cli._message_task

    print("states:", [(i.kind, i.state) for i in cli._drawer.items])
    print("last_track:", g.last_track)
    print("sent:", sent)
    assert [i.state for i in cli._drawer.items] == ["to_interactive"]
    assert g.last_track == "interactive"
    assert sent == ["随便改点东西"], "降级后未回注常驻轨执行"
