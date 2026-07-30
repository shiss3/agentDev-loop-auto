"""交付轨右侧抽屉:需求状态机列表 + 交付消息流水。

DeliveryItem 一行一态(pending/parsing/parsed/executing/done/failed/to_interactive);
DrawerPanel 组合「队列行 + 分隔线 + log_buf 流水」产出 fragments 供 FormattedTextControl。
纯内存组件,invalidate 由 TuiApp 侧触发。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples

from autoloop_agent.chat.content_buffer import ContentBuffer

# 状态图标(静态,不做动画)
_STATE_ICON = {
    "pending": "⏳",
    "parsing": "…",
    "parsed": "✓",
    "executing": "▶",
    "done": "✅",
    "failed": "✗",
    "to_interactive": "↪",
}

PANEL_WIDTH = 44


@dataclass
class DeliveryItem:
    """交付队列项:auto=auto 模式需求;adoption=semi/auto 采纳方案进交付;continuation=@模块名续作。"""

    seq: int
    text: str
    kind: str  # "auto" | "adoption" | "continuation"
    state: str = "pending"
    detail: str = ""  # 调度结果/失败原因摘要(可选,追加在行尾)
    target: str = ""  # continuation: 目标模块 module_id


class DrawerPanel:
    """右侧抽屉面板:队列状态行 + 交付消息流水(复用 ContentBuffer)。"""

    WIDTH = PANEL_WIDTH

    def __init__(self) -> None:
        self.items: list[DeliveryItem] = []
        self.log_buf = ContentBuffer()
        self._next_seq = 1

    @property
    def visible(self) -> bool:
        """有历史项即显示(含全部完成的遗留列表)。"""
        return bool(self.items)

    def add_item(self, text: str, kind: str, *, target: str = "") -> DeliveryItem:
        item = DeliveryItem(seq=self._next_seq, text=text, kind=kind, target=target)
        self._next_seq += 1
        self.items.append(item)
        return item

    def append_log(self, text: str) -> None:
        """交付消息流水(多行文本逐行追加)。"""
        for line in text.splitlines() or [""]:
            self.log_buf.append_plain(line)

    def append_question_card(self, question: str, options: list[str]) -> None:
        """解析反问卡(进流水):边框样式对齐 ContentBuffer.append_question_card。"""
        self.append_log("┌ 解析反问 ──────────────────")
        self.append_log(f"│ {question}")
        for i, opt in enumerate(options, 1):
            self.append_log(f"│  {i}. {opt}")
        self.append_log("└ 输入编号选择，或直接输入自定义答案")

    def _item_row(self, item: DeliveryItem) -> StyleAndTextTuples:
        icon = _STATE_ICON.get(item.state, "?")
        summary = item.text.replace("\n", " ")[:30]
        row = f"{icon} #{item.seq} {summary}"
        if item.detail:
            row += f" ({item.detail})"
        return [("", row)]

    def get_formatted_text(self) -> StyleAndTextTuples:
        """队列行 + 分隔线 + 流水;行间插换行,与 ContentBuffer 同款扁平结构。"""
        fragments: StyleAndTextTuples = []
        rows: list[StyleAndTextTuples] = [[("bold", "─ 交付队列 ─")]]
        for item in self.items:
            rows.append(self._item_row(item))
        rows.append([("", "─" * 20)])
        for row in rows:
            fragments.extend(row)
            fragments.append(("", "\n"))
        fragments.extend(self.log_buf.get_formatted_text())
        return fragments
