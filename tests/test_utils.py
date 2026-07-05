"""_build_message 工具函数测试 — Phase 2 Step 2"""

from __future__ import annotations

from harness_agent.chat.events import ChatEvent, EventType
from harness_agent.core.utils import _build_message


def test_build_message():
    """_build_message('hi') → ChatEvent, type=TEXT, data={'text':'hi'}, agent='default'"""
    ev = _build_message("hi")
    assert isinstance(ev, ChatEvent)
    assert ev.type == EventType.TEXT
    assert ev.data == {"text": "hi"}
    assert ev.agent == "default"


def test_build_message_agent():
    """_build_message('hi', agent='l0').agent == 'l0'"""
    ev = _build_message("hi", agent="l0")
    assert ev.agent == "l0"
