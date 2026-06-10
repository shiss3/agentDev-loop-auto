# TUI 遗留问题追踪

**创建日期**: 2026-06-10  
**最后更新**: 2026-06-10

---

## Issue #1: 滚动条不支持鼠标拖拽

**状态**: 🟡 遗留（未实现）

**问题描述**  
`ScrollbarMargin` 是 prompt_toolkit 的纯视觉组件，仅负责渲染滚动条样式，**不处理任何鼠标事件**。用户无法用鼠标点击滚动条来跳转位置或拖拽滑块。

**影响**  
- 用户必须使用键盘（PageUp/PageDown/Home/End）或滚轮来滚动
- 无法像浏览器一样拖拽滚动条快速定位

**当前可用的滚动方式**：

| 操作 | 行为 |
|------|------|
| `PageUp` / `PageDown` | 翻 20 行 |
| `Home` / `End` | 跳到顶/底部 |
| 鼠标滚轮 | 逐行滚动（需鼠标模式） |
| `F2` | 切换鼠标模式 / 复制模式 |

**修复方案**  
自定义一个支持鼠标事件的 `ScrollbarMargin` 子类，处理 `MOUSE_DOWN` 和 `MOUSE_MOVE` 事件：

```python
from prompt_toolkit.mouse_events import MouseEventType

class ClickableScrollbarMargin(ScrollbarMargin):
    def __init__(self, scroll_controller=None, **kwargs):
        super().__init__(**kwargs)
        self._scroll_controller = scroll_controller  # 提供跳转/滚动方法

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            # 根据点击位置计算目标行
            # 调用 scroll_controller 跳转
            pass
```

**相关文件**  
- `src/harness_agent/chat/tui_app.py`

**参考**  
- `prompt_toolkit/layout/margins.py` - ScrollbarMargin 源码
- `docs/tui_scroll_fix.md` - 滚动机制详细说明

---

*（后续遗留问题在此追加）*
