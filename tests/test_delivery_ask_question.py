"""交付轨解析反问(_delivery_can_use_tool) 单元测试
覆盖：
- 非 AskUserQuestion 工具一律 PermissionResultDeny（保解析只读语义）
- AskUserQuestion 问题卡进 drawer 流水，不进主区 content_buffer
- answer 模式数字/文字映射 answers，回显进 drawer
- 双轨竞态：交互轨 pending 时交付轨提问排队，前答完才占槽渲卡
- ESC 取消：Deny 不悬挂，answer 模式复位
不拉真实 SDK/CLI：session_store 用 MagicMock；ChatCLI 不 start()。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    SessionStore,
)

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.repl import ChatCLI


def make_cli() -> ChatCLI:
    return ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))


def make_question_input() -> dict:
    return {
        "questions": [
            {
                "question": "用哪个方案？",
                "header": "方案",
                "options": [
                    {"label": "方案A", "description": "快"},
                    {"label": "方案B", "description": "稳"},
                ],
                "multiSelect": False,
            },
            {"question": "叫什么名字？", "header": "名", "options": [], "multiSelect": False},
        ]
    }


def buf_text(buf: ContentBuffer) -> str:
    return "".join(frag[1] for frag in buf.get_formatted_text())


async def test_non_ask_tool_denied():
    """解析阶段非 AskUserQuestion 工具一律拒绝（只读语义由 allowed_tools 白名单保证，
    白名单外的工具到回调这里全拒）。"""
    cli = make_cli()
    for tool_name in ("Bash", "Write", "Edit"):
        result = await cli._delivery_can_use_tool(tool_name, {}, None)
        assert isinstance(result, PermissionResultDeny)


async def test_ask_card_into_drawer_not_main_buffer():
    """AskUserQuestion 卡片进 drawer 流水，主区 content_buffer 无卡片。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    drawer_text = buf_text(cli._drawer.log_buf)
    assert "┌ 解析反问" in drawer_text
    assert "用哪个方案？" in drawer_text
    assert "1. 方案A — 快" in drawer_text
    assert "用哪个方案？" not in buf_text(cli.content_buffer)
    cli._cancel_current_request()
    await task


async def test_answer_digit_maps_and_echo_into_drawer():
    """数字映射选项 label，answers 回注，回显进 drawer。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("2")
    await asyncio.sleep(0.05)  # 进入第 2 问
    await cli._on_user_input("自定义名")
    result = await task
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {
        "用哪个方案？": "方案B",
        "叫什么名字？": "自定义名",
    }
    assert "回答> 方案B" in buf_text(cli._drawer.log_buf)


async def test_delivery_ask_queues_behind_interactive():
    """双轨竞态：交互轨提问占用 answer 槽时，交付轨提问排队；
    交互轨答完后交付轨才渲卡占槽。"""
    cli = make_cli()
    t1 = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    # 交付轨排队中：卡片未进 drawer
    assert "┌ 解析反问" not in buf_text(cli._drawer.log_buf)
    # 答完交互轨两问
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    await cli._on_user_input("x")
    await t1
    await asyncio.sleep(0.05)
    # 交付轨占槽：卡片进 drawer
    assert "┌ 解析反问" in buf_text(cli._drawer.log_buf)
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    await cli._on_user_input("y")
    result2 = await t2
    assert result2.updated_input["answers"] == {
        "用哪个方案？": "方案A",
        "叫什么名字？": "y",
    }


async def test_esc_cancel_deny_not_dangling():
    """answer 模式 ESC：Deny 返回不悬挂，槽位/answer 模式复位。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    assert cli._pending_answer is not None
    cli._cancel_current_request()
    result = await asyncio.wait_for(task, timeout=1)
    assert isinstance(result, PermissionResultDeny)
    assert cli._pending_answer is None
    assert not cli.tui.answer_mode


def test_governor_injected_with_delivery_callback():
    """ChatCLI._create_session：Governor 挂 _delivery_can_use_tool 为解析回调。"""
    cli = make_cli()
    assert cli.governor._parse_can_use_tool == cli._delivery_can_use_tool
