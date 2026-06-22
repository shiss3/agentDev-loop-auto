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
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, Dimension, FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

from harness_agent.chat.content_buffer import ContentBuffer
from harness_agent.chat.scroll_controller import ScrollController, _ScrollableFTControl


# ── 样式定义 ──

_TUI_STYLE = Style.from_dict(
    {
        "statusbar": "bg:#1a1a2e",
        "statusbar.model": "#00d4ff",
        "statusbar.separator": "#ffffff",
        "statusbar.info": "#888888",
        "status.input-prefix": "bold",
        # 用户输入样式
        "user-message": "bg:#2d2d2d #cccccc",
    }
)

# Spinner 帧刷新间隔（秒）
_SPINNER_INTERVAL = 0.08

# 输入历史文件路径
_DEFAULT_HISTORY_PATH = str(Path.home() / ".harness" / "chat_history")


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
        mouse_default: bool = True,
    ) -> None:
        self.content_buffer = content_buffer
        self.model_name = model_name
        self._history_file = history_file or _DEFAULT_HISTORY_PATH

        # ── 状态栏 ──
        self._status_text: str = f"{model_name} · 就绪"

        # ── 输入回调 ──
        self._on_submit: Callable[[str], Awaitable[None]] | None = None
        self._on_cancel: Callable[[], None] | None = None  # 取消回调

        # ── Spinner 定时器 ──
        self._spinner_task: asyncio.Task | None = None

        # ── Application（延迟创建，run() 时构建）──
        self._app: Application | None = None
        self._input_buffer: Buffer | None = None
        self._content_window: Window | None = None

        # ── 滚动控制器 ──
        # 用"虚拟光标行号"驱动 vertical_scroll：
        #   prompt_toolkit 的 Window._scroll 每帧会根据 ui_content.cursor_position.y
        #   重写 vertical_scroll；所以我们通过 get_cursor_position 提供这个 y，
        #   _scroll 就会自动把目标行带入可视区域。
        # 跟随模式判定见 ScrollController.is_following()
        self._scroll = ScrollController(
            get_fragments=self.content_buffer.get_formatted_text,
            invalidate=self._invalidate,
        )

        # ── 内容变更回调 → invalidate（需在 _scroll 创建后注册）──
        self.content_buffer.set_on_change(self._scroll.on_content_change)

        # ── 鼠标捕获开关（F2 切换） ──
        # mouse_support=True 会让终端把鼠标事件转发给应用，导致无法用左键拖拽复制。
        # 提供 F2 一键切换：复制模式（关闭捕获）↔ 鼠标模式（开启滚轮）。
        # 用动态 Condition 替代静态 bool —— prompt_toolkit Renderer 每帧会读取，
        # 切换 self._mouse_enabled 后下一帧会自动发送 enable/disable mouse 转义序列。
        self._mouse_enabled: bool = mouse_default
        # 启动提示只在首次 run() 时打印一次
        self._mouse_hint_printed: bool = False

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

    def set_on_cancel(self, handler: Callable[[], None]) -> None:
        """设置取消回调"""
        self._on_cancel = handler

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
        # 首次启动：在内容区追加一行 onboarding 提示，告知用户两条复制路径
        if not self._mouse_hint_printed:
            self.content_buffer.append_plain(
                "提示：鼠标滚轮可滚动；复制文本请按 F2 进入复制模式（或按住 Shift 拖拽）"
            )
            self._mouse_hint_printed = True
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
            text=self._scroll.get_content_fragments,
            focusable=False,
            get_cursor_position=self._scroll.get_cursor_position,
            on_cursor_down=self._scroll.cursor_down_one,
            on_cursor_up=self._scroll.cursor_up_one,
        )
        content_window = Window(
            content=content_ctrl,
            wrap_lines=True,
            # 内容区占据剩余空间（min=3 确保至少能看到几行，max=None 表示无上限）
            height=Dimension(min=3, max=None),
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
            # 动态 mouse_support：每帧由 Renderer 读取，F2 切换后自动生效
            mouse_support=Condition(lambda: self._mouse_enabled),
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
            self._scroll.page_up()

        @kb.add("pagedown")
        def _page_down(event):
            """PageDown: 向下翻 20 行（到达末尾即恢复自动跟随）"""
            self._scroll.page_down()

        @kb.add("home")
        def _to_home(event):
            """Home: 跳到内容顶部"""
            self._scroll.to_home()

        @kb.add("end")
        def _to_end(event):
            """End: 跳到内容末尾，恢复自动跟随"""
            self._scroll.to_end()

        @kb.add("f2")
        def _toggle_mouse(event):
            """F2: 切换 鼠标模式 ↔ 复制模式

            鼠标模式：滚轮可滚动，但终端会吞掉左键拖拽，无法系统级选中。
            复制模式：关闭鼠标捕获，左键可正常拖拽选中复制；滚动改用 PageUp/PageDown/Home/End。
            切换通过翻转 self._mouse_enabled —— Renderer 下一帧读取 Condition 后
            会自动发送 enable/disable mouse 转义序列。
            """
            self._mouse_enabled = not self._mouse_enabled
            self._invalidate()

        @kb.add("escape")
        def _cancel_request(event):
            """ESC: 取消当前请求"""
            if self._on_cancel:
                self._on_cancel()

        return kb

    def _on_buffer_accept(self, buffer: Buffer) -> None:
        """Buffer accept 回调（同步）→ 调度异步 handler"""
        text = buffer.text.strip()
        if not text:
            return

        buffer.text = ""

        # 用户输入后，立即强制滚动到末尾
        # 确保在 Agent 输出之前，光标已经在正确位置
        self._scroll.scroll_to_end()

        if self._on_submit:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._on_submit(text))
            except RuntimeError:
                pass

    # ── 内部：渲染辅助 ──

    def _get_status_fragments(self) -> StyleAndTextTuples:
        """返回状态栏的 fragment 列表

        结构：[业务状态] + [鼠标模式 hint]
        模式 hint 作为独立段拼接到末尾，不会被 set_status_running / set_status_ready 覆盖。
        """
        if self._mouse_enabled:
            mode_hint = "  [🖱 鼠标已启用 · F2 进入复制模式]"
        else:
            mode_hint = "  [📋 复制模式 · 可拖拽选中 · F2 退出]"

        return FormattedText([
            ("class:statusbar.model", f" {self._status_text}"),
            ("class:statusbar.info", mode_hint),
        ])

    def _invalidate(self) -> None:
        """触发 TUI 重绘"""
        if self._app:
            try:
                self._app.invalidate()
            except Exception:
                pass
