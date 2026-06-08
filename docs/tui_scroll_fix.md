# TUI 内容区滚动修复方案

## 问题描述

`harness chat` TUI 模式下，内容区存在三个滚动相关问题：

1. **内容不能滚动** — 超出可视区域的内容被截断，只能通过拉长终端窗口才能看到
2. **无输出跟随** — Agent 流式输出新内容时，视图不会自动滚动到底部
3. **无手动滚动** — 没有 PageUp/PageDown 键绑定，鼠标滚轮也被禁用

## 根因分析

核心代码位于 `src/harness_agent/chat/tui_app.py` 的 `_build_app()` 方法。

### 内容区 Window 定义（修复前）

```python
content_window = Window(
    content=content_ctrl,
    wrap_lines=True,
    height=Dimension(min=1),
)
```

### 缺陷清单

| # | 缺陷 | 位置 | 影响 |
|---|------|------|------|
| ① | 无 `always_hide_cursor` | `Window(...)` 构造参数 | `FormattedTextControl` 是 `focusable=False`，但 Window 仍尝试管理光标位置，导致 viewport 行为异常 |
| ② | `vertical_scroll` 永远为 0 | `_on_content_change` 只调 `invalidate()` | 重绘时 viewport 不移动，始终从第 0 行渲染，超出可视区域的内容被截断 |
| ③ | 无滚动键绑定 | `_build_key_bindings` 仅注册了 `Ctrl+C` | 用户无法手动翻页查看历史输出 |
| ④ | `mouse_support=False` | `Application(...)` 构造参数 | 鼠标滚轮事件被丢弃 |

### 机制说明

prompt_toolkit 的 `Window` 通过 `vertical_scroll` 整数属性控制渲染起始行：

```
Window 渲染流程:
  1. 从 self.vertical_scroll 行开始读取内容
  2. 渲染 visible_height 行（由 HSplit 布局分配）
  3. 超出部分截断

默认值: vertical_scroll = 0（始终从顶部渲染）
问题: 没有任何代码修改此值 → 永远看到头部内容
```

`HSplit` 布局分配：
- `input_window` (height=1) — 固定 1 行
- `status_window` (height=1) — 固定 1 行
- `content_window` (min=1) — 占据剩余所有行 = `终端高度 - 2`

## 修复方案

### 仅修改一个文件：`src/harness_agent/chat/tui_app.py`

### 改动 1：存储内容区 Window 引用

`__init__` 中新增属性：

```python
self._content_window: Window | None = None
```

### 改动 2：内容区 Window 加 `always_hide_cursor` + 保存引用

```python
content_window = Window(
    content=content_ctrl,
    wrap_lines=True,
    always_hide_cursor=True,          # ← 新增：无光标模式，viewport 行为正确
    height=Dimension(min=1),
)
self._content_window = content_window  # ← 新增：保存引用供滚动管理使用
```

### 改动 3：新增 `_scroll_to_bottom()` 方法

```python
def _scroll_to_bottom(self) -> None:
    """将内容区滚动到底部（跟随最新输出）"""
    if self._content_window:
        # 设置极大值，prompt_toolkit 渲染时自动 clamp 到有效范围
        # 即 max(0, total_lines - visible_height)
        self._content_window.vertical_scroll = 999999
```

### 改动 4：修改 `_on_content_change` — 先滚动再重绘

```python
# 修复前
def _on_content_change(self) -> None:
    self._invalidate()

# 修复后
def _on_content_change(self) -> None:
    self._scroll_to_bottom()   # ← 先更新滚动位置
    self._invalidate()          # ← 再触发重绘
```

### 改动 5：新增 PageUp / PageDown 键绑定

```python
@kb.add("pageup")
def _page_up(event):
    """PageUp: 内容区向上翻页"""
    if self._content_window:
        self._content_window.vertical_scroll = max(
            0, self._content_window.vertical_scroll - 20
        )
        self._invalidate()

@kb.add("pagedown")
def _page_down(event):
    """PageDown: 内容区向下翻页"""
    if self._content_window:
        self._content_window.vertical_scroll += 20
        self._invalidate()
```

### 改动 6：启用鼠标滚轮

```python
# 修复前
mouse_support=False,

# 修复后
mouse_support=True,
```

## 数据流

```
用户输入 / Agent 输出
    │
    ▼
ContentBuffer.append_text() / append_tool_line() / ...
    │
    ▼
ContentBuffer._notify()
    │
    ▼
TuiApp._on_content_change()
    │
    ├─→ _scroll_to_bottom()          # vertical_scroll = 999999
    │
    └─→ _invalidate()                # 标记需要重绘
              │
              ▼
         prompt_toolkit 渲染循环
              │
              ├─ 读取 vertical_scroll = 999999
              ├─ clamp 到 max(0, total_lines - visible_height)
              └─ 从该位置渲染 visible_height 行
              │
              ▼
         用户看到最新输出 ✓
```

## 交互方式

| 操作 | 行为 |
|------|------|
| 内容自动增长 | 自动滚动到底部（输出跟随） |
| `PageUp` | 向上翻 20 行（查看历史输出） |
| `PageDown` | 向下翻 20 行（回到最新输出） |
| 鼠标滚轮 | prompt_toolkit 内建滚动（需 `mouse_support=True`） |
| `Ctrl+C` | 退出（不变） |

## 已知限制

1. **流式输出打断阅读（scroll lock）** — 用户手动 PageUp 查看历史时，流式新内容到达仍会强制滚到底部。
   修复思路：不需要额外标志位，在 `_scroll_to_bottom()` 中判断当前视口是否已在底部附近，只有接近底部时才跟随滚动：

   ```python
   def _scroll_to_bottom(self) -> None:
       if self._content_window:
           w = self._content_window
           # prompt_toolkit 渲染后会 clamp vertical_scroll 到 max(0, total - visible)
           # 如果用户没有手动上翻，vertical_scroll 应该等于上一次的 clamp 值（即底部）
           # 用户按 PageUp 后，vertical_scroll 会比 clamp 值小至少 20
           #
           # 利用这个特性：先临时设为极大值读出 clamp 后的"底部位置"，
           # 再对比当前视口位置决定是否跟随。
           #
           # 但因为 clamp 发生在渲染阶段（我们无法在 _scroll_to_bottom 中同步获取），
           # 实际最简方案是：用一个 _at_bottom 布尔量，PageUp 置 False，PageDown 置 True。
           # 本质只有两行代码的改动：
           if self._at_bottom:
               w.vertical_scroll = 999999
   ```
   其中 `self._at_bottom` 在 `__init__` 中初始化为 `True`，PageUp handler 置 `False`，PageDown handler 置 `True`。
   流式输出期间不会误触发，只有用户主动上翻时才暂停跟随。
2. **翻页步进固定 20 行** — 理想方案是根据 content_window 实际高度动态计算，但需要访问渲染时的 layout info，当前固定步进已足够使用。
3. **vertical_scroll = 999999 技巧** — 依赖 prompt_toolkit 的 clamp 行为。这是 prompt_toolkit 社区常用的惯用写法，API 稳定。

---

# 第二轮修复 — 根因纠正：仍然只显示一小部分

## 现象（修复后仍存在）

按上一轮方案修改后，问题没有解决：

- 内容超出窗口时仍只显示开头一小部分
- 拉长 PowerShell 窗口能显示更多 → 说明 Window 永远从 source line 0 开始渲染
- `_scroll_to_bottom()` 里设置 `vertical_scroll = 999999` 看似有效，但**渲染时被悄悄重置回 0**
- PageUp/PageDown 也几乎无效

## 根因：prompt_toolkit 的 `vertical_scroll` 不是写一下就生效的

通读 `prompt_toolkit==3.0.52` 的 `layout/containers.py` 后，渲染流程是：

```
Window.write_to_screen
    └─ _write_to_screen_at_index
         └─ _scroll(ui_content, ...)           ← 关键：会重写 vertical_scroll！
              ├─ wrap_lines=True  → _scroll_when_linewrapping  (containers.py:2316)
              └─ wrap_lines=False → _scroll_without_linewrapping (containers.py:2437)
         └─ _copy_body(..., self.vertical_scroll, ...)   ← 用 _scroll 改写后的值渲染
```

`_scroll(...)` 不是"把 vertical_scroll 当输入读"，而是**输入+输出双向**：每次渲染都会**根据 `ui_content.cursor_position.y` 重写 `vertical_scroll`，确保光标可见**。

### 致命链条

在 `tui_app.py` 里，内容区用的是：

```python
content_ctrl = FormattedTextControl(
    text=self.content_buffer.get_formatted_text,
    focusable=False,                # ← 关键
)
```

而 `FormattedTextControl.create_content` (controls.py:372-420) 在没有 `[SetCursorPosition]` 标记、也没有 `get_cursor_position` 回调时：

```python
self.cursor_position = cursor_position or Point(x=0, y=0)    # controls.py:162
```

→ `ui_content.cursor_position.y == 0`

然后回到 `_scroll`：

- **`wrap_lines=True` 分支**（containers.py:2431）：
  ```python
  self.vertical_scroll = min(self.vertical_scroll, get_max_vertical_scroll())
  ```
  而 `get_max_vertical_scroll()` 是从 `cursor_position.y`（= 0）回溯计算的 → **max 就是 0**。
  无论我们写 `vertical_scroll = 999999` 还是 `123`，这一行都会把它**夹回 0**。

- **`wrap_lines=False` 分支**（containers.py:2492-2493）：
  ```python
  if current_scroll > cursor_pos - scroll_offset_start:
      current_scroll = max(0, cursor_pos - scroll_offset_start)
  ```
  `cursor_pos = 0` → 同样**钳到 0**。

### 结论

> 上一轮的 `vertical_scroll = 999999` + `always_hide_cursor=True` + PageUp/PageDown 全部**白写**了：
> 只要内容区 control 是 `focusable=False` 且没有真实光标，`_scroll` 每帧都会把 `vertical_scroll` 拉回 0，永远只能看到内容顶部。
>
> "拉长窗口能多看几行"也是同一个原因 —— Window 永远从 line 0 开始画，窗口高度越大画得越多，但起点不变。

## 解决方案

要让 `vertical_scroll` 实际生效，必须让 `_scroll` "相信"光标在我们希望的位置。三种可行做法，从简单到复杂：

### 方案 A（推荐）：把内容区改成可滚动的 Buffer

让内容区不再是只读的 `FormattedTextControl`，而是一个不可编辑的 `Buffer` + `BufferControl`。`Buffer` 自带 `cursor_position`，`_scroll` 会正常按光标位置工作，prompt_toolkit 内置的 `scroll_page_up` / `scroll_page_down` 也能直接使用。

**改动量大**，需要重写 `ContentBuffer` 的渲染管线（fragment → 纯文本，丢失部分样式），不适合本项目当前结构。

### 方案 B（推荐）：给 `FormattedTextControl` 提供 `get_cursor_position` 回调

让 `_scroll` 看到一个动态可控的"虚拟光标"位置，由我们告诉它"光标在内容最底部"或"光标在用户上翻到的那一行"。这样 `_scroll` 就会按这个位置去 clamp `vertical_scroll`，**反而是我们利用 `_scroll` 来定位**。

**改动量小、风险低**，是本次推荐方案。

### 方案 C：override `Window.get_vertical_scroll`

`Window` 构造参数支持 `get_vertical_scroll: Callable[[Window], int]`。但**这个钩子只在 `_scroll_without_linewrapping` 中被调用**（containers.py:2502-2504），`_scroll_when_linewrapping` 分支不读取。我们需要 `wrap_lines=True`（不然中文/长行会被截断而不是换行），所以此钩子**对当前场景无效**，舍弃。

---

## 最终方案 — 采用方案 B

### 思路

1. 给 `FormattedTextControl` 传 `get_cursor_position` 回调，返回一个 `Point(x=0, y=<目标行>)`
2. 维护一个 `self._cursor_line: int`，表示"虚拟光标"应该在第几行
3. 自动跟随时，`_cursor_line` 永远指向"内容最后一行的索引"
4. PageUp / PageDown 时，调整 `_cursor_line`（同时关闭/开启自动跟随）
5. 渲染时 `_scroll` 会**自动**根据这个 y 计算合理的 `vertical_scroll`，并把目标行带到可视区下沿
6. 鼠标滚轮的 `_scroll_down` / `_scroll_up`（containers.py:2559/2572）会调 `content.move_cursor_down/up`；`FormattedTextControl` 默认没有这两个方法 → 需要补一个轻量子类提供它们

### 改动清单（仅 `src/harness_agent/chat/tui_app.py`）

#### 改动 1：在 `__init__` 增加状态

```python
self._cursor_line: int = 0     # 虚拟光标行号（用于驱动 vertical_scroll）
self._line_count: int = 1      # 内容总行数（每次重渲染时更新）
# 删除 _at_bottom 这个标志位 —— 直接用 _cursor_line == _line_count - 1 判断
```

#### 改动 2：包装内容文本回调，统计行数

```python
def _get_content_fragments(self):
    fragments = self.content_buffer.get_formatted_text()
    # 统计 \n 数量来得到行数（fragment 是 [(style, text), ...]）
    text = "".join(t for _, t, *_ in fragments)
    self._line_count = max(1, text.count("\n") + 1)
    return fragments
```

#### 改动 3：定义 `get_cursor_position` 回调

```python
from prompt_toolkit.data_structures import Point

def _get_content_cursor_position(self) -> Point:
    # 虚拟光标停在 _cursor_line，让 _scroll 把它带到可视区
    y = max(0, min(self._cursor_line, self._line_count - 1))
    return Point(x=0, y=y)
```

#### 改动 4：给 FormattedTextControl 装上回调，并自定义一个支持 cursor 移动的子类

```python
class _ScrollableFTControl(FormattedTextControl):
    """支持鼠标滚轮 _scroll_down/_scroll_up 调用 move_cursor_* 的子类"""
    def __init__(self, *args, on_cursor_down=None, on_cursor_up=None, **kw):
        super().__init__(*args, **kw)
        self._on_cursor_down = on_cursor_down
        self._on_cursor_up = on_cursor_up

    def move_cursor_down(self):
        if self._on_cursor_down:
            self._on_cursor_down()

    def move_cursor_up(self):
        if self._on_cursor_up:
            self._on_cursor_up()

# _build_app 中：
content_ctrl = _ScrollableFTControl(
    text=self._get_content_fragments,
    focusable=False,
    get_cursor_position=self._get_content_cursor_position,
    on_cursor_down=self._cursor_down_one,
    on_cursor_up=self._cursor_up_one,
)
```

#### 改动 5：替换原来的 `_scroll_to_bottom` / PageUp / PageDown

```python
def _on_content_change(self) -> None:
    # 跟随模式：把虚拟光标钉在最后一行，_scroll 会自动滚到底
    if self._is_following():
        self._cursor_line = 10**9   # _get_content_cursor_position 会 clamp
    self._invalidate()

def _is_following(self) -> bool:
    # 上次 cursor 已经在最末行附近，就视为跟随状态
    return self._cursor_line >= self._line_count - 1

def _cursor_down_one(self) -> None:
    self._cursor_line = min(self._line_count - 1, self._cursor_line + 1)

def _cursor_up_one(self) -> None:
    self._cursor_line = max(0, self._cursor_line - 1)
```

PageUp / PageDown：

```python
@kb.add("pageup")
def _page_up(event):
    self._cursor_line = max(0, self._cursor_line - 20)
    self._invalidate()

@kb.add("pagedown")
def _page_down(event):
    self._cursor_line = min(self._line_count - 1, self._cursor_line + 20)
    self._invalidate()

@kb.add("end")
def _to_end(event):
    self._cursor_line = self._line_count - 1
    self._invalidate()

@kb.add("home")
def _to_home(event):
    self._cursor_line = 0
    self._invalidate()
```

#### 改动 6：彻底删除上一轮的两个无效写法

```python
# 删除：always_hide_cursor=True              ← 现在我们就要 cursor 参与计算
# 删除：self._content_window.vertical_scroll = 999999   ← 完全无用，必删
```

> 注意：`vertical_scroll` 这条赋值**必须删除**，否则即使新方案生效，赋值后下一帧 `_scroll` 看到一个超大的 `vertical_scroll` 也只是按光标位置 clamp 回去，是无害但有误导性的死代码。

## 验证机制

为什么这次能成功？

1. `FormattedTextControl.create_content` 的 controls.py:162 现在会用我们提供的 `get_cursor_position` 返回值 → `ui_content.cursor_position.y = _cursor_line`
2. `_scroll_when_linewrapping`（containers.py:2316）会根据这个真实 y **正确**计算 `vertical_scroll`：把目标行带入可视区域，自动处理 wrap 后的视觉行数
3. PageUp 改变的不再是 `vertical_scroll`（会被 clamp），而是 `_cursor_line`（不会被任何东西覆写）
4. 鼠标滚轮事件触发 `Window._scroll_down`（containers.py:2559），它会调 `content.move_cursor_down()` → 我们的子类把它路由到 `_cursor_down_one()` → `_cursor_line += 1` → 下一帧渲染时光标到了新位置，`vertical_scroll` 跟着推进

## 交互一览（修复后）

| 操作 | 行为 |
|------|------|
| 内容自动增长 | `_cursor_line` 推到末尾，`_scroll` 自动把末尾带入可视区（输出跟随） |
| `PageUp` / `PageDown` | 调整 `_cursor_line` ±20 行；上翻自动暂停跟随，下翻回到末尾自动恢复 |
| `Home` / `End` | 跳到顶部 / 跳到末尾 |
| 鼠标滚轮 | 通过 `_ScrollableFTControl.move_cursor_up/down` 调整 `_cursor_line` |
| `Ctrl+C` | 退出（不变） |

## 经验教训

prompt_toolkit 的 `Window.vertical_scroll` 是**派生量**，不是**控制量**。
直接写它不会持久 —— 渲染时 `_scroll` 永远会按 `cursor_position` 把它重新算出来。
要控制滚动位置，必须控制 **`ui_content.cursor_position.y`**，让 `_scroll` 替我们算 `vertical_scroll`。

---

# 第三轮修复 — Mouse Trap：开启鼠标后无法复制文本

## 现象

第二轮把 `mouse_support=True` 打开后，鼠标滚轮、PageUp/PageDown、内容自动跟随都正常工作了。
但出现新问题：**无法用鼠标左键拖拽选择文本进行复制**。

对一个 AI 辅助编程 TUI 而言，从内容区把 Agent 输出的代码、错误日志、命令拷出去是**最高频**的操作之一。
当前行为相当于在每个复制动作前都加了一个隐藏前置键（Shift），用户极易踩坑。

## 根因

`mouse_support=True` 让 prompt_toolkit 向终端发送 `DECSET 1000/1002/1006` 序列（`input/vt100_parser.py`、`renderer.py` 中的 `ALTERNATE_SCREEN` + mouse tracking 开关），
告诉终端把所有鼠标事件（按下、移动、滚轮）以**转义序列**形式回传给应用，而不是交给终端自己处理。

一旦终端进入 mouse tracking 模式：

| 行为 | 没开 mouse_support | 开了 mouse_support |
|------|---------------------|---------------------|
| 左键拖拽选中 | ✅ 终端处理，可复制 | ❌ 被应用吞掉 |
| 右键菜单（粘贴） | ✅ 终端处理 | ❌ 被应用吞掉 |
| 滚轮 | ❌ 应用收不到 | ✅ 应用收到 |
| Shift+拖拽 | ✅ | ✅（终端开了 mouse bypass） |

各终端实现差异：
- **Windows Terminal / VS Code 终端**：必须按住 `Shift` 才能绕过应用做系统级选择
- **iTerm2 (macOS)**：默认 `Option` 或 `Cmd`（看版本），新版也支持 `Shift`
- **GNOME Terminal / Konsole**：`Shift`

对刚切到 TUI 的用户而言，"为什么我选不中字"会是非常陡的体验断崖。

## 解决方向梳理

| 方案 | 思路 | 评价 |
|------|------|------|
| A. 不要鼠标，回到 `False` | 放弃滚轮，靠 PageUp/PageDown/Home/End | ❌ 滚轮是大多数用户最自然的滚动方式，舍弃太可惜 |
| B. 用户教育（提示 Shift+拖拽） | 在状态栏写一行 hint | ⚠ 治标不治本，仍要按额外键 |
| C. **运行时切换 mouse_support** | 默认关，按某个键临时进入"滚动模式"再关回去 | ✅ 可行，但状态切换成本高、易迷失 |
| D. **mouse_support 默认开，加一个"复制模式"切换键** | 一键关掉鼠标捕获，再按一次开回来 | ✅✅ 与现代 IDE 终端体验最接近，本次推荐 |
| E. 仅捕获滚轮，不捕获按下/拖拽 | 在 mouse handler 里只处理 SCROLL_UP/DOWN | ⚠ prompt_toolkit 在协议层就开了 button-event tracking，仍会拦截左键 |

方案 D 是终极方案；但它依赖 prompt_toolkit 是否能在**应用运行时**切换 `mouse_support`（默认值是 `Application` 构造参数）。

经查 `prompt_toolkit==3.0.52` 源码：
- `Application.__init__` 把 `mouse_support` 存成 `self.mouse_support`（`application/application.py`）
- 此值在每次 `Renderer.render` 时被读取，决定是否发送 `enable_mouse` / `disable_mouse` 转义序列
- 它接受 `FilterOrBool`，所以可以传一个**动态 Filter**，每次渲染时回调判断

→ **可以运行时切换**。只需把 `mouse_support` 从 `True` 改为 `Condition(lambda: self._mouse_enabled)`，配一个开关键即可。

## 最终方案

### 思路

1. 默认 `mouse_support` **开启**（保持滚轮可用，覆盖 80% 的使用场景）
2. 提供一个**复制模式**：按一次热键 → 关闭鼠标捕获 → 用户用鼠标自由选中/复制 → 再按一次热键 → 恢复鼠标捕获
3. 在状态栏明确显示当前模式（`📋 复制模式` / `🖱 鼠标已启用`）
4. 提供两条退路：
   - 单字符快捷键：`F2`（不冲突任何输入；Ctrl+M = Enter 在终端是别名，必须避开）
   - 命令式：用户可在配置里把默认值设成 `False`，彻底关掉鼠标
5. 复制模式下，仍可用 PageUp/PageDown/Home/End 滚动 —— **滚动 ≠ 必须用鼠标**

### 改动清单（仅 `src/harness_agent/chat/tui_app.py`）

#### 改动 1：新增鼠标开关状态

`__init__`：

```python
self._mouse_enabled: bool = True   # 默认开启鼠标支持
```

构造参数加一个开关，让上游可以默认关掉：

```python
def __init__(
    self,
    content_buffer: ContentBuffer,
    model_name: str = "default",
    history_file: str | None = None,
    mouse_default: bool = True,    # ← 新增
) -> None:
    ...
    self._mouse_enabled = mouse_default
```

#### 改动 2：把 `mouse_support` 改成动态 Filter

```python
from prompt_toolkit.filters import Condition

# _build_app 中：
return Application(
    layout=layout,
    style=_TUI_STYLE,
    key_bindings=kb,
    full_screen=True,
    mouse_support=Condition(lambda: self._mouse_enabled),   # ← 动态
)
```

`Condition` 每帧被 Renderer 读取；切换 `self._mouse_enabled` 后下一帧会自动发送对应的 enable/disable mouse 转义序列。

#### 改动 3：F2 切换 + Shift+F2 永久关闭

`_build_key_bindings` 中追加：

```python
@kb.add("f2")
def _toggle_mouse(event):
    """F2: 临时切换鼠标捕获（复制模式 ↔ 鼠标模式）"""
    self._mouse_enabled = not self._mouse_enabled
    self._update_status_for_mouse()
    self._invalidate()
```

#### 改动 4：状态栏显示当前模式

```python
def _update_status_for_mouse(self) -> None:
    if self._mouse_enabled:
        self.set_status_text(f"{self.model_name} · 🖱 鼠标已启用（F2 进入复制模式）")
    else:
        self.set_status_text(f"{self.model_name} · 📋 复制模式：可自由选中文本（F2 退出）")
```

> ⚠ 注意：`set_status_running` / `set_status_ready` 也要保留这个 hint 的位置，
> 否则一旦 Agent 开始运行，状态栏被覆盖，用户会忘记自己在哪个模式。
> 最稳妥做法是把"模式"作为状态栏 **独立** 一段（拼接到末尾），不与运行/就绪互相覆盖。

更彻底的写法是把状态栏拆成两段：

```python
def _get_status_fragments(self) -> StyleAndTextTuples:
    mode_hint = "  [📋 复制模式 F2]" if not self._mouse_enabled else ""
    return FormattedText([
        ("class:statusbar.model", f" {self._status_text}"),
        ("class:statusbar.info", mode_hint),
    ])
```

这样 `_status_text` 只承载业务状态，模式 hint 不会被运行/就绪刷新覆盖。

#### 改动 5：首次启动提示

在 `run()` 开头往内容区追加一行提示（仅一次）：

```python
self.content_buffer.append_system_line(
    "提示：鼠标滚轮可滚动内容；复制文本请按 F2 进入复制模式（或在该模式外按住 Shift 拖拽）"
)
```

让用户不需要读文档就知道两条路：F2 / Shift+拖拽。

### 关键决策点

1. **为什么默认开鼠标，而不是默认关？**
   滚轮是绝大多数现代用户的第一直觉。如果默认关，新用户根本不会发现有滚轮支持；
   而"无法复制"在用户**第一次想复制**时才暴露 —— 此时状态栏的 F2 提示会立刻给出答案。
   两害相权取其轻：先给最自然的体验，把"切换"成本留给低频但明确的需求。

2. **为什么用 F2，不用 Ctrl+L / Esc / Alt+M？**
   - `Ctrl+L`：传统是清屏；shell 用户肌肉记忆冲突
   - `Esc`：prompt_toolkit 内部用作 Meta 前缀，按下后会等待 500ms 看是否有后续键
   - `Alt+M`：部分终端会把 Alt 发送为 `\e` 前缀，与 Esc 一样有歧义
   - `F2`：在 TUI 里几乎没人占用，单键，无歧义。备选 `F4`

3. **为什么不在 mouse handler 里"只处理滚轮事件，放过其它"？**
   prompt_toolkit 的 mouse tracking 协议层一旦开启就是全开（DECSET 1002 是 button-event），
   即使应用代码 return `NotImplemented`，按下/拖拽**仍然不会**回到终端。
   无法在协议层做"只收滚轮"，只能在整段开关之间切换。

4. **为什么不依赖 Shift+拖拽？**
   - 跨终端不一致（前文有列）
   - 不是显眼的 affordance —— 普通用户根本不会想到
   - 在禁用 Shift 直通的部分终端（如某些 SSH 客户端嵌套环境）完全失效

### 交互一览（第三轮修复后）

| 操作 | 行为 |
|------|------|
| 鼠标滚轮 | 滚动内容（默认模式下） |
| 左键拖拽 | 默认模式：被 TUI 吞掉；按 F2 后：原生选择/复制 |
| `F2` | 切换 复制模式 ↔ 鼠标模式 |
| `PageUp` / `PageDown` / `Home` / `End` | 两种模式下都可用 |
| 状态栏 | 显示当前模式提示 |
| `Shift + 拖拽` | 终端 bypass，两种模式下都可（保底退路） |
| `Ctrl+C` | 退出（不变） |

## 经验教训（补充）

`mouse_support` 不是一个简单的"功能开关"，它是**对终端鼠标控制权的独占请求**。
任何 TUI 一旦打开它，都必须想好"用户怎么复制文本"。
prompt_toolkit 的 `FilterOrBool` 支持让我们能把这个开关变成可切换状态，
配合一个显眼的快捷键（F2），就能兼顾"滚轮自然"和"复制自由"两个看似冲突的体验目标。

派生原则：**任何会改变终端默认交互的特性（鼠标、备用屏、原生光标），都必须提供一个用户可见、可逆的开关。**
