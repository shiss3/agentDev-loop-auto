"""交付轨 FIFO 队列 + 双轨分流单元测试
覆盖:
- auto 输入进交付队列(不再走 _handle_message),FIFO 串行
- adoption 输入(采用方案)进交付队列走 adopt_flow
- auto 解析判 interactive -> to_interactive 回注交互轨(forced,不二次解析)
- 单项异常标 failed 不阻塞后续
- ESC 只取消当前项,队列剩余继续
不拉真实 SDK/CLI:Governor 三段 flow 全 mock。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import SessionStore

from autoloop_agent.chat.events import text_event
from autoloop_agent.chat.repl import ChatCLI


def make_cli(track: str = "delivery") -> ChatCLI:
    cli = ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))
    g = MagicMock()
    g.is_adoption_input = lambda t: t.strip() == "采用方案"
    g.last_track = track
    g.last_spec = {"task_summary": "x"}
    g.parse_calls: list[str] = []

    async def parse_flow(text):
        g.parse_calls.append(text)
        yield text_event(f"parse {text}")

    async def deliver_flow(spec):
        yield text_event("deliver")

    async def adopt_flow():
        yield text_event("adopt")

    async def handle_user_input(text, *, forced_track=None):
        g.injected = (text, forced_track)
        if False:
            yield

    g.parse_flow = parse_flow
    g.deliver_flow = deliver_flow
    g.adopt_flow = adopt_flow
    g.handle_user_input = handle_user_input
    cli.governor = g
    return cli


async def settle(cli: ChatCLI) -> None:
    """等交付 runner 跑完 + 让注入 task 起步。"""
    if cli._delivery_runner is not None:
        await cli._delivery_runner
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_auto_input_enqueued_fifo():
    cli = make_cli()
    await cli._on_user_input("需求A")
    await cli._on_user_input("需求B")
    await settle(cli)
    g = cli.governor
    assert g.parse_calls == ["需求A", "需求B"]  # FIFO 顺序
    assert [i.state for i in cli._drawer.items] == ["done", "done"]


@pytest.mark.asyncio
async def test_auto_input_not_sent_to_handle_message():
    cli = make_cli()
    called = []
    orig = cli._handle_message

    async def spy(text, **kw):
        called.append(text)

    cli._handle_message = spy
    await cli._on_user_input("需求A")
    await settle(cli)
    assert called == []  # auto 输入不走交互轨


@pytest.mark.asyncio
async def test_adoption_routes_to_adopt_flow():
    cli = make_cli()
    adopted = []
    orig = cli.governor.adopt_flow

    async def spy():
        adopted.append(1)
        async for e in orig():
            yield e

    cli.governor.adopt_flow = spy
    await cli._on_user_input("采用方案")
    await settle(cli)
    assert adopted == [1]
    assert cli._drawer.items[0].kind == "adoption"
    assert cli._drawer.items[0].state == "done"


@pytest.mark.asyncio
async def test_auto_to_interactive_injects_without_reparse():
    cli = make_cli(track="interactive")
    await cli._on_user_input("需求A")
    await settle(cli)
    # 等注入 task 收尾
    if cli._message_task is not None:
        await cli._message_task
    assert cli._drawer.items[0].state == "to_interactive"
    assert cli.governor.injected == ("需求A", "interactive")


@pytest.mark.asyncio
async def test_item_failure_does_not_block_queue():
    cli = make_cli()

    async def bad_parse(text):
        if text == "需求A":
            raise RuntimeError("boom")
        yield text_event("ok")

    cli.governor.parse_flow = bad_parse
    await cli._on_user_input("需求A")
    await cli._on_user_input("需求B")
    await settle(cli)
    states = [i.state for i in cli._drawer.items]
    assert states[0] == "failed"
    assert states[1] == "done"


@pytest.mark.asyncio
async def test_esc_cancels_only_current_item():
    cli = make_cli()
    gate = asyncio.Event()
    calls = []

    async def deliver_flow(spec):
        calls.append(1)
        if len(calls) == 1:
            await gate.wait()  # 第一项卡在交付,永不放行
        yield text_event("d")

    cli.governor.deliver_flow = deliver_flow
    await cli._on_user_input("需求A")
    await cli._on_user_input("需求B")
    await asyncio.sleep(0.1)  # A 进入交付卡住
    cli._cancel_current_request()  # ESC:只取消当前项
    await settle(cli)
    states = [i.state for i in cli._drawer.items]
    assert states[0] == "failed"
    assert cli._drawer.items[0].detail == "已取消"
    assert states[1] == "done"  # 队列剩余继续
