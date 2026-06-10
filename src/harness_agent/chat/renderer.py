"""ChatRenderer — TUI 渲染器

消费 ChatEvent，驱动 TuiApp + ContentBuffer。
不直接操作终端，所有输出通过 TuiApp 组件完成。

Phase 2.5 策略：流式阶段纯文本追加，Turn End 后暂不做 Markdown 精渲染。
"""

from __future__ import annotations

from harness_agent.chat.events import ChatEvent, EventType
from harness_agent.chat.content_buffer import ContentBuffer
from harness_agent.chat.tui_app import TuiApp


# 工具名 → 图标
_TOOL_ICONS = {
    "Read": "📖",
    "Write": "✏️",
    "Edit": "📝",
    "Bash": "⚡",
    "Grep": "🔍",
    "Glob": "📂",
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "🛠️")


class ChatRenderer:
    """消费 ChatEvent 流，驱动 TuiApp

    与 Phase 1 的 TerminalRenderer 对比：
      Phase 1: console.print() → 直接输出到终端
      Phase 2: tui.content_buffer.append_*() → 通过 prompt_toolkit 渲染

    接口不变：handle(event: ChatEvent) -> None
    """

    def __init__(self, tui: TuiApp) -> None:
        self.tui = tui
        self.buf: ContentBuffer = tui.content_buffer
        self.turn_tool_count: int = 0
        self.total_tool_count: int = 0

        # Turn 内统计（供 Turn End 使用）
        self._turn_input_tokens: int = 0
        self._turn_output_tokens: int = 0
        self._turn_start_time: float = 0.0

    # ── 公共入口 ──

    def handle(self, event: ChatEvent) -> None:
        """分发事件到对应的渲染方法"""
        handler = {
            EventType.TEXT: self._render_text,
            EventType.TOOL_USE: self._render_tool_use,
            EventType.TOOL_RESULT: self._render_tool_result,
            EventType.TURN_START: self._render_turn_start,
            EventType.TURN_END: self._render_turn_end,
            EventType.THINKING: self._render_thinking,
            EventType.ERROR: self._render_error,
            EventType.USAGE: self._render_usage,
            EventType.CANCELLED: self._render_cancelled,
        }.get(event.type)

        if handler:
            handler(event)

    # ── 事件处理 ──

    def _render_turn_start(self, event: ChatEvent) -> None:
        """Turn 开始：追加用户输入 + 启动 Spinner"""
        import time

        self.turn_tool_count = 0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_start_time = time.monotonic()

        prompt = event.data.get("prompt", "")
        self.buf.append_user_input(prompt)
        self.tui.start_spinner("思考中...")
        self.tui.set_status_running()

    def _render_text(self, event: ChatEvent) -> None:
        """Agent 文本输出：纯文本流式追加"""
        self.tui.stop_spinner()
        text = event.data.get("text", "")
        if text:
            self.buf.append_text(text)

    def _render_tool_use(self, event: ChatEvent) -> None:
        """工具调用：停止 Spinner → 单行工具日志 → 重启 Spinner"""
        self.tui.stop_spinner()

        self.turn_tool_count += 1
        self.total_tool_count += 1

        tool_name = event.data.get("tool_name", "?")
        tool_input = event.data.get("tool_input", "")
        icon = _tool_icon(tool_name)

        self.buf.append_tool_line(
            icon=icon,
            index=self.total_tool_count,
            tool_name=tool_name,
            summary=tool_input,
            status="running",
        )
        self.tui.start_spinner("执行中...")

    def _render_tool_result(self, event: ChatEvent) -> None:
        """工具结果：停止 Spinner → 更新最后一行工具状态"""
        self.tui.stop_spinner()

        is_error = event.data.get("is_error", False)
        # 目前 ContentBuffer 不支持 retroactively 更新已追加行的状态
        # 作为简化，追加一个状态行
        if is_error:
            content = event.data.get("content", "")[:200]
            self.buf.append_plain(f"  ✗ 失败: {content}")
        # 成功时不额外输出（工具行本身的 running 状态已足够表达）

    def _render_turn_end(self, event: ChatEvent) -> None:
        """Turn 结束：停止 Spinner → 更新状态栏统计"""
        self.tui.stop_spinner()

        duration_s = round(
            (event.data.get("duration_ms", 0) / 1000), 1
        )

        self.tui.set_status_ready(
            tool_count=self.turn_tool_count,
            input_tokens=self._turn_input_tokens,
            output_tokens=self._turn_output_tokens,
            duration_s=duration_s,
        )
        self.tui.focus_input()

    def _render_thinking(self, event: ChatEvent) -> None:
        """Agent 思考中"""
        self.tui.start_spinner("思考中...")

    def _render_error(self, event: ChatEvent) -> None:
        """错误：停止 Spinner → 追加错误行"""
        self.tui.stop_spinner()
        error = event.data.get("error", "未知错误")[:500]
        self.buf.append_error(error)
        self.tui.set_status_text(f"{self.tui.model_name} · 错误")
        self.tui.focus_input()

    def _render_cancelled(self, event: ChatEvent) -> None:
        """取消：停止 Spinner → 追加取消提示"""
        self.tui.stop_spinner()
        reason = event.data.get("reason", "用户取消")
        self.buf.append_plain(f"⚠️ 已取消: {reason}")
        # 状态恢复为就绪，方便用户继续输入
        self.tui.set_status_text(f"{self.tui.model_name} · 就绪")
        self.tui.focus_input()

    def _render_usage(self, event: ChatEvent) -> None:
        """Token 用量：暂存统计，Turn End 时一起显示

        同时提取实际模型名称并更新 TUI 状态栏。
        """
        self._turn_input_tokens += event.data.get("input_tokens", 0)
        self._turn_output_tokens += event.data.get("output_tokens", 0)

        # 如果有实际模型名称，更新 TUI 显示
        model_name = event.data.get("model_name")
        if model_name and model_name != self.tui.model_name:
            self.tui.model_name = model_name

    # ── 辅助渲染（供斜杠命令等使用）──

    def render_welcome(self, project_dir: str, version: str = "0.1.0") -> None:
        """渲染欢迎信息"""
        self.buf.append_plain(f" harness Agent v{version} · 工作区: {project_dir}")
        self.buf.append_blank_line()
        self.tui.set_status_text(f"{self.tui.model_name} · 就绪")

    def render_goodbye(self, stats: dict) -> None:
        """渲染退出信息"""
        turns = stats.get("turn_count", 0)
        tools = stats.get("total_tool_calls", 0)
        self.buf.append_blank_line()
        self.buf.append_plain(f"本次会话: {turns} 轮对话 · {tools} 次工具调用")

    def render_command_result(self, text: str) -> None:
        """渲染斜杠命令结果"""
        self.buf.append_plain(text)
