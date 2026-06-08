"""TuiApp — 全屏 TUI 应用封装

职责：
1. 构建 prompt_toolkit Application + Layout（内容区 / 输入框 / 状态栏）
2. 管理输入 Buffer（历史、accept 回调）
3. 管理状态栏文本
4. 管理 Spinner 定时器
5. 提供 invalidate 触发重绘

绝不做的事：
- ❌ 了解 ChatEvent 类型
- ❌ 管理 SDK 会话
- ❌ 处理业务逻辑
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, Dimension, FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

from harness_agent.chat.content_buffer import ContentBuffer


# ── 样式定义 ──

_TUI_STYLE = Style.from_dict(
    {
        "statusbar": "bg:#1a1a2e",
        "statusbar.model": "#00d4ff",
        "statusbar.separator": "#ffffff",
        "statusbar.info": "#888888",
        "status.input-prefix": "bold",
    }
)

# Spinner 帧刷新间隔（秒）
_SPINNER_INTERVAL = 0.08

# 输入历史文件路径
_DEFAULT_HISTORY_PATH = str(Path.home() / ".harness" / "chat_history")


class _ScrollableFTControl(FormattedTextControl):
    """支持鼠标滚轮 _scroll_down/_scroll_up 调用 move_cursor_* 的 FormattedTextControl

    prompt_toolkit Window._scroll_down/_scroll_up（containers.py:2559/2572）
    会调 content.move_cursor_down()/move_cursor_up()。FormattedTextControl
    默认不实现这两个方法 → 鼠标滚轮事件无效。本子类把它们路由到外部回调，
    使外部可以通过修改"虚拟光标行号"驱动滚动。
    """

    def __init__(self, *args, on_cursor_down=None, on_cursor_up=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_cursor_down = on_cursor_down
        self._on_cursor_up = on_cursor_up

    def move_cursor_down(self) -> None:
        if self._on_cursor_down:
            self._on_cursor_down()

    def move_cursor_up(self) -> None:
        if self._on_cursor_up:
            self._on_cursor_up()


class TuiApp:
    """全屏 TUI 应用

    Layout 结构:
      HSplit([
        content_window   ← ContentBuffer 驱动，自动滚动
        input_window     ← Buffer + "You > " 前缀，单行输入
        status_window    ← 模型名 + 状态信息
      ])

    使用方式:
        tui = TuiApp(content_buffer=buf, model_name="my-model")
        tui.set_on_submit(my_async_handler)
        await tui.run()
    """

    def __init__(
        self,
        content_buffer: ContentBuffer,
        model_name: str = "default",
        history_file: str | None = None,
    ) -> None:
        self.content_buffer = content_buffer
        self.model_name = model_name
        self._history_file = history_file or _DEFAULT_HISTORY_PATH

        # ── 内容变更回调 → invalidate ──
        self.content_buffer.set_on_change(self._on_content_change)

        # ── 状态栏 ──
        self._status_text: str = f"{model_name} · 就绪"

        # ── 输入回调 ──
        self._on_submit: Callable[[str], Awaitable[None]] | None = None

        # ── Spinner 定时器 ──
        self._spinner_task: asyncio.Task | None = None

        # ── Application（延迟创建，run() 时构建）──
        self._app: Application | None = None
        self._input_buffer: Buffer | None = None
        self._content_window: Window | None = None

        # ── 滚动状态 ──
        # 用"虚拟光标行号"驱动 vertical_scroll：
        #   prompt_toolkit 的 Window._scroll 每帧会根据 ui_content.cursor_position.y
        #   重写 vertical_scroll；所以我们通过 get_cursor_position 提供这个 y，
        #   _scroll 就会自动把目标行带入可视区域。
        # 跟随模式判定：_cursor_line >= _line_count - 1
        self._cursor_line: int = 0
        self._line_count: int = 1

    # ── 属性 ──

    @property
    def app(self) -> Application:
        """获取 Application 实例（首次访问时创建）"""
        if self._app is None:
            self._app = self._build_app()
        return self._app

    # ── 状态栏 ──

    def set_status_text(self, text: str) -> None:
        """设置状态栏文本"""
        self._status_text = text
        self._invalidate()

    def set_status_running(self) -> None:
        """状态栏切换为运行中"""
        self._status_text = f"{self.model_name} · 运行中..."
        self._invalidate()

    def set_status_ready(
        self,
        tool_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        """状态栏切换为就绪，显示统计信息"""
        parts = [self.model_name]

        if tool_count > 0:
            parts.append(f"{tool_count} 次工具")

        if input_tokens > 0 or output_tokens > 0:
            in_k = f"{input_tokens / 1000:.1f}k" if input_tokens >= 1000 else str(input_tokens)
            out_k = f"{output_tokens / 1000:.1f}k" if output_tokens >= 1000 else str(output_tokens)
            parts.append(f"in={in_k} out={out_k}")

        if duration_s > 0:
            parts.append(f"⏱️ {duration_s:.1f}s")

        self._status_text = " · ".join(parts)
        self._invalidate()

    # ── Spinner 管理 ──

    def start_spinner(self, text: str = "思考中...") -> None:
        """启动 Spinner（同时启动定时器）"""
        self.content_buffer.start_spinner(text)
        self._start_spinner_timer()

    def stop_spinner(self) -> None:
        """停止 Spinner（同时停止定时器）"""
        self.content_buffer.stop_spinner()
        self._stop_spinner_timer()

    def _start_spinner_timer(self) -> None:
        """启动 Spinner 帧更新定时器"""
        self._stop_spinner_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spinner_task = loop.create_task(self._spinner_loop())

    def _stop_spinner_timer(self) -> None:
        """停止 Spinner 帧更新定时器"""
        if self._spinner_task and not self._spinner_task.done():
            self._spinner_task.cancel()
        self._spinner_task = None

    async def _spinner_loop(self) -> None:
        """Spinner 帧更新循环"""
        try:
            while True:
                await asyncio.sleep(_SPINNER_INTERVAL)
                self.content_buffer.tick_spinner()
                self._invalidate()
        except asyncio.CancelledError:
            pass

    # ── 输入管理 ──

    def set_on_submit(self, handler: Callable[[str], Awaitable[None]]) -> None:
        """设置用户输入回调"""
        self._on_submit = handler

    def focus_input(self) -> None:
        """聚焦输入框"""
        if self._app and self._input_buffer:
            # 在 layout 中找到 input window 并 focus
            try:
                self.app.layout.focus(self._input_buffer)
            except Exception:
                pass

    # ── 生命周期 ──

    async def run(self) -> None:
        """启动 TUI 应用（阻塞直到退出）"""
        app = self.app
        try:
            await app.run_async()
        finally:
            self._stop_spinner_timer()

    def exit(self) -> None:
        """退出 TUI 应用"""
        if self._app:
            self._stop_spinner_timer()
            self._app.exit()

    # ── 内部：Application 构建 ──

    def _build_app(self) -> Application:
        """构建 prompt_toolkit Application"""
        # 输入 Buffer
        history = self._make_history()
        self._input_buffer = Buffer(
            history=history,
            enable_history_search=True,
            multiline=False,
            accept_handler=self._on_buffer_accept,
        )

        # 内容区
        content_ctrl = _ScrollableFTControl(
            text=self._get_content_fragments,
            focusable=False,
            get_cursor_position=self._get_content_cursor_position,
            on_cursor_down=self._cursor_down_one,
            on_cursor_up=self._cursor_up_one,
        )
        content_window = Window(
            content=content_ctrl,
            wrap_lines=True,
            # 让内容区占据剩余空间（减去输入行和状态栏）
            height=Dimension(min=1),
        )
        self._content_window = content_window

        # 输入区
        input_ctrl = BufferControl(
            buffer=self._input_buffer,
            focusable=True,
        )
        input_window = Window(
            content=input_ctrl,
            height=1,
            get_line_prefix=lambda lineno, wrap_count: FormattedText(
                [("class:status.input-prefix", "You > ")]
            ),
        )

        # 状态栏
        status_ctrl = FormattedTextControl(
            text=self._get_status_fragments,
            focusable=False,
        )
        status_window = Window(
            content=status_ctrl,
            height=1,
            style="class:statusbar",
        )

        # 整体布局
        root = HSplit([content_window, input_window, status_window])
        layout = Layout(root)

        # 初始焦点：输入框
        layout.focus(self._input_buffer)

        # 键盘绑定
        kb = self._build_key_bindings()

        return Application(
            layout=layout,
            style=_TUI_STYLE,
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
        )

    def _make_history(self):
        """创建输入历史"""
        try:
            Path(self._history_file).parent.mkdir(parents=True, exist_ok=True)
            return FileHistory(self._history_file)
        except Exception:
            return InMemoryHistory()

    def _build_key_bindings(self) -> KeyBindings:
        """全局键盘绑定"""
        kb = KeyBindings()

        @kb.add("c-c")
        def _interrupt(event):
            """Ctrl+C: 退出应用"""
            self.exit()

        @kb.add("pageup")
        def _page_up(event):
            """PageUp: 向上翻 20 行（暂停自动跟随）"""
            self._cursor_line = max(0, self._cursor_line - 20)
            self._invalidate()

        @kb.add("pagedown")
        def _page_down(event):
            """PageDown: 向下翻 20 行（到达末尾即恢复自动跟随）"""
            self._cursor_line = min(self._line_count - 1, self._cursor_line + 20)
            self._invalidate()

        @kb.add("home")
        def _to_home(event):
            """Home: 跳到内容顶部"""
            self._cursor_line = 0
            self._invalidate()

        @kb.add("end")
        def _to_end(event):
            """End: 跳到内容末尾，恢复自动跟随"""
            self._cursor_line = max(0, self._line_count - 1)
            self._invalidate()

        return kb

    def _on_buffer_accept(self, buffer: Buffer) -> None:
        """Buffer accept 回调（同步）→ 调度异步 handler"""
        text = buffer.text.strip()
        if not text:
            return

        buffer.text = ""

        if self._on_submit:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._on_submit(text))
            except RuntimeError:
                pass

    # ── 内部：渲染辅助 ──

    def _get_status_fragments(self) -> StyleAndTextTuples:
        """返回状态栏的 fragment 列表"""
        return FormattedText([
            ("class:statusbar.model", f" {self._status_text}"),
        ])

    def _on_content_change(self) -> None:
        """ContentBuffer 变更回调 → 跟随模式下把虚拟光标钉在末尾，触发重绘"""
        if self._is_following():
            # 给一个大值，下次 _get_content_cursor_position 会 clamp 到 line_count-1
            self._cursor_line = 10**9
        self._invalidate()

    # ── 内部：内容滚动管理 ──

    def _get_content_fragments(self) -> StyleAndTextTuples:
        """内容区文本回调：取 fragments 同时统计总行数（用于光标 clamp）"""
        fragments = self.content_buffer.get_formatted_text()
        # fragment 元组结构是 (style, text) 或 (style, text, handler)
        text = "".join(item[1] for item in fragments)
        self._line_count = max(1, text.count("\n") + 1)
        return fragments

    def _get_content_cursor_position(self) -> Point:
        """提供给 FormattedTextControl 的虚拟光标位置

        prompt_toolkit Window._scroll 会根据这个 y 值反算 vertical_scroll，
        把目标行带入可视区域 → 我们以此控制滚动位置。
        """
        y = max(0, min(self._cursor_line, self._line_count - 1))
        return Point(x=0, y=y)

    def _is_following(self) -> bool:
        """虚拟光标是否在内容末尾（即处于自动跟随状态）"""
        return self._cursor_line >= self._line_count - 1

    def _cursor_down_one(self) -> None:
        """鼠标滚轮向下 → 虚拟光标下移一行"""
        self._cursor_line = min(self._line_count - 1, self._cursor_line + 1)

    def _cursor_up_one(self) -> None:
        """鼠标滚轮向上 → 虚拟光标上移一行"""
        self._cursor_line = max(0, self._cursor_line - 1)

    def _invalidate(self) -> None:
        """触发 TUI 重绘"""
        if self._app:
            try:
                self._app.invalidate()
            except Exception:
                pass
