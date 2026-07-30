"""交互轨权限放开 + AskUserQuestion TUI 反问 单元测试
覆盖：
- can_use_tool 对非 AskUserQuestion 工具直接 PermissionResultAllow（无交互）
- AskUserQuestion 触发问题卡渲染进 ContentBuffer 滚动历史
- answer 模式数字/文字输入正确映射 updated_input['answers']（多问题逐个回答）
- answer 模式 ESC 取消整个消息：pending Future 被取消不悬挂，前缀/路由恢复
- _build_resident_session 配置断言：permission_mode="default"（ask 态必触发回调；
  验收原文写 bypassPermissions，实测该模式下回调不触发已裁决改 default）+
  allowed_tools=[] 无白名单裁剪 + can_use_tool 挂载（含 rebuild 后仍生效）
- TuiApp answer 模式输入前缀 'You > ' ↔ '回答> '
不拉真实 SDK/CLI：session_store 用 MagicMock；ChatCLI 不 start()（不 spawn 子进程）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    SessionStore,
)

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.repl import ChatCLI
from autoloop_agent.chat.tui_app import TuiApp
from autoloop_agent.core.architect import Governor, _default_can_use_tool
from autoloop_agent.core.base_session import BaseAgentSession


# ── 辅助 ──────────────────────────────────────────────

def make_cli() -> ChatCLI:
    """构造 ChatCLI（mock session_store 避免真实 mkdir；不 start，不拉 SDK）。"""
    return ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))


def make_question_input() -> dict:
    """两问 AskUserQuestion tool_input：第 1 问带选项，第 2 问无选项。"""
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


def buffer_text(buf: ContentBuffer) -> str:
    """拍平 ContentBuffer 全部 fragment 文本（断言用）。"""
    return "".join(frag[1] for frag in buf.get_formatted_text())


# ── 1. 非 AskUserQuestion 直接 allow ──

async def test_non_ask_tool_direct_allow():
    """非 AskUserQuestion 工具直接 PermissionResultAllow，无交互副作用：
    不渲染问题卡、不进 answer 模式、无 pending Future、updated_input 不改写。"""
    cli = make_cli()
    result = await cli._can_use_tool("Bash", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is None
    assert cli._pending_answer is None
    assert not cli.tui.answer_mode
    assert "Agent 提问" not in buffer_text(cli.content_buffer)


async def test_non_ask_tool_direct_allow_write_edit():
    """写工具（Write/Edit）同样直接 allow（交互轨权限放开，不问用户）。"""
    cli = make_cli()
    for tool_name in ("Write", "Edit", "Glob"):
        result = await cli._can_use_tool(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)


# ── 2. 问题卡渲染进 ContentBuffer ──

def test_question_card_rendered_into_buffer():
    """append_question_card：卡片头/问题/编号选项/footer 全进滚动历史。"""
    buf = ContentBuffer()
    buf.append_question_card("用哪个方案？", ["方案A — 快", "方案B — 稳"])
    text = buffer_text(buf)
    assert "┌ Agent 提问" in text
    assert "用哪个方案？" in text
    assert "1. 方案A — 快" in text
    assert "2. 方案B — 稳" in text
    assert "└" in text


async def test_ask_triggers_card_render():
    """AskUserQuestion 回调触发问题卡渲染（问题+选项进 ContentBuffer）。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    text = buffer_text(cli.content_buffer)
    assert "┌ Agent 提问" in text
    assert "用哪个方案？" in text
    assert "1. 方案A — 快" in text
    assert "2. 方案B — 稳" in text
    # 收尾：取消避免悬挂
    cli._cancel_current_request()
    await task


# ── 3. answer 模式数字/文字映射 ──

async def test_answer_digit_maps_to_option_label():
    """纯数字输入 → 对应编号选项 label；answers 键为问题原文。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    assert cli._pending_answer is not None
    await cli._on_user_input("2")
    await asyncio.sleep(0.05)  # 进入第 2 问
    await cli._on_user_input("自定义名")
    result = await task
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {
        "用哪个方案？": "方案B",
        "叫什么名字？": "自定义名",
    }
    # 原始 tool_input 字段保留（questions 不丢）
    assert result.updated_input["questions"] == make_question_input()["questions"]


async def test_answer_text_maps_to_custom():
    """文字输入 → 自定义答案原样入 answers。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("都不用，重来")  # 文字 -> 自定义（非数字不映射选项）
    await asyncio.sleep(0.05)
    await cli._on_user_input("3")  # 第 2 问无选项，数字越界 -> 按自定义文字
    result = await task
    assert result.updated_input["answers"] == {
        "用哪个方案？": "都不用，重来",
        "叫什么名字？": "3",
    }


async def test_unicode_digit_treated_as_custom_text():
    """Unicode 数字（'²'，isdigit=True 但 int() 拒收）按自定义文字处理，不崩溃。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("²")  # 不映射选项、不 ValueError
    await asyncio.sleep(0.05)
    await cli._on_user_input("x")
    result = await task
    assert result.updated_input["answers"]["用哪个方案？"] == "²"


async def test_multi_question_sequential_cards():
    """多问题逐个渲染逐个回答：第 1 问答完才渲第 2 问卡片。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    text_after_q1 = buffer_text(cli.content_buffer)
    assert "用哪个方案？" in text_after_q1
    assert "叫什么名字？" not in text_after_q1  # 第 2 问未渲
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    text_after_q2 = buffer_text(cli.content_buffer)
    assert "叫什么名字？" in text_after_q2  # 第 2 问已渲
    await cli._on_user_input("x")
    await task


async def test_answer_echoed_to_buffer():
    """回答回显进滚动历史（回答> 前缀）。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("1")
    assert "回答> 方案A" in buffer_text(cli.content_buffer)
    await asyncio.sleep(0.05)
    await cli._on_user_input("n")
    await task


# ── 4. answer 模式前缀切换 ──

def test_answer_mode_prefix_switch():
    """answer 模式输入前缀 'You > ' ↔ '回答> '。"""
    tui = TuiApp(content_buffer=ContentBuffer())
    assert tui._get_input_prefix()[0][1] == "You > "
    tui.set_answer_mode(True)
    assert tui.answer_mode
    assert tui._get_input_prefix()[0][1] == "回答> "
    tui.set_answer_mode(False)
    assert tui._get_input_prefix()[0][1] == "You > "


async def test_answer_mode_during_question():
    """提问期间 answer_mode=True，答完恢复 False。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    assert cli.tui.answer_mode
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    assert cli.tui.answer_mode  # 第 2 问仍在 answer 模式
    await cli._on_user_input("x")
    await task
    assert not cli.tui.answer_mode


# ── 5. ESC 取消语义 ──

async def test_esc_cancels_message_and_future_not_dangling():
    """answer 模式 ESC：取消整个消息，pending Future 被取消不悬挂，
    回调返回 PermissionResultDeny，answer 模式与前缀恢复。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    assert cli._pending_answer is not None
    cli._cancel_current_request()  # ESC
    result = await asyncio.wait_for(task, timeout=1)  # 不悬挂：能返回
    assert isinstance(result, PermissionResultDeny)
    assert cli._pending_answer is None
    assert cli._pending_options == []
    assert not cli.tui.answer_mode
    assert cli.tui._get_input_prefix()[0][1] == "You > "


async def test_routing_restored_after_esc():
    """ESC 取消后后续输入路由恢复正常（不再喂 Future，走普通消息路径）。"""
    cli = make_cli()
    cli.mode = "semi"  # auto 模式普通输入进交付队列(双轨),本测试走交互轨路径
    cli._handle_message = AsyncMock()  # 防真发 SDK
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    cli._cancel_current_request()
    await task
    await cli._on_user_input("普通消息")
    cli._handle_message.assert_awaited_once_with("普通消息")


async def test_pending_input_routed_to_future_not_message():
    """pending 期间输入不触发 _handle_message（路由给 Future）。"""
    cli = make_cli()
    cli._handle_message = AsyncMock()
    task = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("1")
    cli._handle_message.assert_not_called()
    await asyncio.sleep(0.05)
    await cli._on_user_input("x")
    await task


# ── 6. 常驻执行体配置断言 ──

def test_resident_session_permission_config():
    """_build_resident_session：permission_mode='default'（ask 态必触发回调；
    bypassPermissions 下回调不触发已裁决弃用）+ allowed_tools=[] 无白名单裁剪
    + can_use_tool 挂载进 options。"""
    governor = Governor(project_dir=".", session_store=MagicMock(spec=SessionStore))
    session = governor.session
    assert session.permission_mode == "default"
    assert session.allowed_tools == []
    assert session.can_use_tool is not None
    opts = session._build_options()
    assert opts.permission_mode == "default"
    assert opts.allowed_tools == []
    assert opts.can_use_tool is session.can_use_tool


def test_resident_session_can_use_tool_injectable():
    """can_use_tool 可从 chat 层注入 Governor → 常驻执行体。"""
    callback = AsyncMock()
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        can_use_tool=callback,
    )
    assert governor.session.can_use_tool is callback
    assert governor.session._build_options().can_use_tool is callback


async def test_rebuild_retains_injected_can_use_tool(monkeypatch):
    """rebuild 重建后注入的 can_use_tool 仍生效。"""
    callback = AsyncMock()
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        can_use_tool=callback,
    )
    monkeypatch.setattr(BaseAgentSession, "start", AsyncMock())
    monkeypatch.setattr(BaseAgentSession, "close", AsyncMock())
    await governor.rebuild()
    assert governor.session.can_use_tool is callback


# ── 7. Governor 默认回调（headless 降级，无 chat 层注入时）──

async def test_default_callback_allows_non_ask():
    """默认回调：非 AskUserQuestion 直接 allow（无用户交互）。"""
    result = await _default_can_use_tool("Bash", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultAllow)


async def test_default_callback_denies_ask():
    """默认回调：AskUserQuestion 拒绝（无 TUI 通道无法向用户提问，不悬挂）。"""
    result = await _default_can_use_tool("AskUserQuestion", make_question_input(), None)
    assert isinstance(result, PermissionResultDeny)
