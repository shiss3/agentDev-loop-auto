"""DrawerPanel 抽屉面板单元测试
覆盖:
- add_item 序号自增 + visible 条件(空隐藏,有项即显)
- 状态行渲染(图标/序号/摘要/detail)
- append_log 多行拆分
"""
from __future__ import annotations

from autoloop_agent.chat.drawer import DrawerPanel


def _flat(panel: DrawerPanel) -> str:
    return "".join(frag[1] for frag in panel.get_formatted_text())


def test_empty_panel_hidden():
    panel = DrawerPanel()
    assert panel.visible is False


def test_add_item_seq_and_visible():
    panel = DrawerPanel()
    a = panel.add_item("需求A", "auto")
    b = panel.add_item("需求B", "adoption")
    assert (a.seq, b.seq) == (1, 2)
    assert a.state == "pending"
    assert panel.visible is True


def test_item_rows_render_states():
    panel = DrawerPanel()
    a = panel.add_item("做一个登录页", "auto")
    b = panel.add_item("采用方案", "adoption")
    b.state = "executing"
    text = _flat(panel)
    assert "交付队列" in text
    assert "⏳ #1 做一个登录页" in text
    assert "▶ #2 采用方案" in text
    a.state = "done"
    assert "✅ #1" in _flat(panel)


def test_item_detail_appended():
    panel = DrawerPanel()
    item = panel.add_item("x", "auto")
    item.state = "failed"
    item.detail = "已取消"
    assert "✗ #1 x (已取消)" in _flat(panel)


def test_append_log_multiline_split():
    panel = DrawerPanel()
    panel.append_log("第一行\n第二行")
    text = _flat(panel)
    assert "第一行" in text and "第二行" in text


def test_log_below_queue_rows():
    panel = DrawerPanel()
    panel.add_item("需求", "auto")
    panel.append_log("LOG-MARK")
    text = _flat(panel)
    assert text.index("#1") < text.index("LOG-MARK")


def test_append_question_card_into_log():
    """解析反问卡：头部/问题/编号选项/footer 全进抽屉流水。"""
    from autoloop_agent.chat.drawer import DrawerPanel
    drawer = DrawerPanel()
    drawer.append_question_card("用哪个方案？", ["方案A — 快", "方案B — 稳"])
    text = "".join(frag[1] for frag in drawer.log_buf.get_formatted_text())
    assert "┌ 解析反问" in text
    assert "用哪个方案？" in text
    assert "1. 方案A — 快" in text
    assert "2. 方案B — 稳" in text
    assert "└" in text
