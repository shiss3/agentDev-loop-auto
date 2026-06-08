"""ChatRenderer — 增强终端渲染器

Phase 1 的 TerminalRenderer 做了什么：
  - 简单 console.print 文本
  - 基本的工具名展示

Phase 2 的 ChatRenderer 新增：
  - 流式纯文本渲染（无事后精渲染，避免双重打印）
  - Spinner 动画（Agent 思考时）
  - 工具调用折叠显示（名称 + 状态图标，输入可展开）
  - Turn 起止分隔线
  - Token 用量统计面板
  - 累计耗时
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live

from harness_agent.chat.events import ChatEvent, EventType


class ChatRenderer:
    """消费 ChatEvent 流，渲染到终端"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.turn_tool_count = 0
        self.total_tool_count = 0
        self._spinner_live: Live | None = None

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
        }.get(event.type)

        if handler:
            handler(event)

    # ── 各事件类型的渲染实现 ──

    def _render_turn_start(self, event: ChatEvent) -> None:
        """渲染一轮对话的开始"""
        self.turn_tool_count = 0
        self._stop_spinner()
        self.console.print()
        # 启动思考动画
        self._start_spinner()

    def _render_text(self, event: ChatEvent) -> None:
        """渲染 Agent 文本输出 — 纯文本流式输出，无双重打印"""
        self._stop_spinner()
        text = event.data.get("text", "")
        self.console.print(text, end="", highlight=False)

    def _render_tool_use(self, event: ChatEvent) -> None:
        """渲染工具调用 — 紧凑的单行显示"""
        self._stop_spinner()
        self.turn_tool_count += 1
        self.total_tool_count += 1

        tool_name = event.data.get("tool_name", "?")
        tool_input = event.data.get("tool_input", "")

        # 工具图标映射
        icon = _tool_icon(tool_name)

        self.console.print(
            f"  [dim]{icon} [{self.total_tool_count}] "
            f"[bold]{tool_name}[/bold][/dim]"
            f" [dim italic]{tool_input}[/dim italic]"
        )

        # 工具执行期间显示 spinner
        self._start_spinner("  执行中...")

    def _render_tool_result(self, event: ChatEvent) -> None:
        """渲染工具执行结果"""
        self._stop_spinner()
        is_error = event.data.get("is_error", False)
        content = event.data.get("content", "")[:200]

        if is_error:
            self.console.print(f"  [red]✗ 失败[/red] [dim]{content}[/dim]")
        else:
            self.console.print(f"  [green]✓ 完成[/green]")

    def _render_turn_end(self, event: ChatEvent) -> None:
        """渲染一轮对话结束 — 统计信息"""
        self._stop_spinner()

        tool_count = event.data.get("tool_count", 0)
        duration_ms = event.data.get("duration_ms", 0)

        parts = []
        if tool_count > 0:
            parts.append(f"🛠️ {tool_count} 次工具调用")
        if duration_ms > 0:
            seconds = duration_ms / 1000
            parts.append(f"⏱️ {seconds:.1f}s")

        if parts:
            summary = " · ".join(parts)
            self.console.print(f"\n  [dim]{summary}[/dim]")

        self.console.print()

    def _render_thinking(self, event: ChatEvent) -> None:
        """渲染思考状态"""
        self._start_spinner()

    def _render_error(self, event: ChatEvent) -> None:
        """渲染错误"""
        self._stop_spinner()
        error = event.data.get("error", "未知错误")[:500]
        self.console.print(
            Panel(
                f"[red]{error}[/red]",
                title="💥 错误",
                border_style="red",
                expand=False,
            )
        )

    def _render_usage(self, event: ChatEvent) -> None:
        """渲染 Token 用量"""
        input_t = event.data.get("input_tokens", 0)
        output_t = event.data.get("output_tokens", 0)
        cache_r = event.data.get("cache_read_tokens", 0)
        cache_c = event.data.get("cache_creation_tokens", 0)

        parts = [f"📊 in={input_t} out={output_t}"]
        if cache_r > 0:
            parts.append(f"cache_read={cache_r}")
        if cache_c > 0:
            parts.append(f"cache_create={cache_c}")

        self.console.print(f"  [dim]{' '.join(parts)}[/dim]")

    # ── Spinner 管理 ──

    def _start_spinner(self, text: str = "  思考中...") -> None:
        """启动思考动画"""
        if self._spinner_live is not None:
            return  # 已经在转了
        spinner = Spinner("dots", text=text, style="dim")
        self._spinner_live = Live(spinner, console=self.console, refresh_per_second=10)
        self._spinner_live.start()

    def _stop_spinner(self) -> None:
        """停止思考动画"""
        if self._spinner_live is not None:
            self._spinner_live.stop()
            self._spinner_live = None

    # ── 辅助渲染 ──

    def render_welcome(self, project_dir: str) -> None:
        """渲染欢迎信息"""
        self.console.print(
            Panel(
                "[bold cyan]🐴 Harness Agent — Chat Mode[/bold cyan]\n\n"
                f"[dim]项目:[/dim] {project_dir}\n"
                "[dim]输入消息开始对话，输入 [bold]/help[/bold] 查看命令[/dim]",
                border_style="cyan",
                expand=False,
            )
        )
        self.console.print()

    def render_goodbye(self, stats: dict) -> None:
        """渲染退出信息"""
        turns = stats.get("turn_count", 0)
        tools = stats.get("total_tool_calls", 0)
        self.console.print()
        self.console.print(
            Panel(
                f"[dim]本次会话: {turns} 轮对话 · {tools} 次工具调用[/dim]",
                title="👋 再见",
                border_style="dim",
                expand=False,
            )
        )

    def render_command_result(self, text: str) -> None:
        """渲染斜杠命令结果"""
        self.console.print(f"  {text}")


def _tool_icon(tool_name: str) -> str:
    """工具名 → 图标"""
    icons = {
        "Read": "📖",
        "Write": "✏️",
        "Edit": "📝",
        "Bash": "⚡",
        "Grep": "🔍",
        "Glob": "📂",
    }
    return icons.get(tool_name, "🛠️")
