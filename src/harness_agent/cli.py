"""CLI 入口 — 流式渲染层

关键职责：
1. 加载 .env 环境变量
2. 解析用户命令
3. 订阅 LangGraph astream_events，实时渲染到终端
4. 绝不涉及 Agent 逻辑
"""

import asyncio
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from harness_agent.core.orchestrator import HarnessOrchestrator

# 加载 .env
load_dotenv()

console = Console()


# ── 事件渲染器 ──────────────────────────────────────────

class TerminalRenderer:
    """将 astream_events 中的自定义事件渲染到终端

    事件类型与渲染规则：
    ┌──────────────┬─────────────────────────────────────┐
    │ agent_text   │ 直接打印文本（流式追加）              │
    │ agent_tool   │ 打印 🛠️ 工具名 + 摘要输入            │
    │ agent_result │ 打印 ✅ 成功 或 ❌ 失败 + 截断内容    │
    │ agent_error  │ 打印 💥 错误详情                     │
    └──────────────┴─────────────────────────────────────┘
    """

    def __init__(self):
        self.tool_count = 0

    def handle_event(self, event: dict):
        kind = event.get("event")
        name = event.get("name", "")

        if kind != "on_custom_event":
            return

        data = event.get("data", {})

        if name == "agent_text":
            console.print(data.get("text", ""), end="")

        elif name == "agent_tool":
            self.tool_count += 1
            tool_name = data.get("tool_name", "unknown")
            tool_input = data.get("tool_input", "")
            console.print(
                f"\n  [dim]🛠️  [{self.tool_count}] {tool_name}[/dim]"
                f" [dim italic]{tool_input}[/dim italic]"
            )

        elif name == "agent_result":
            is_error = data.get("is_error", False)
            content = data.get("content", "")[:200]
            if is_error:
                console.print(f"  [red]❌ 失败:[/red] [dim]{content}[/dim]")
            else:
                console.print(f"  [green]✅ 完成[/green]")

        elif name == "agent_error":
            error = data.get("error", "")[:300]
            console.print(f"\n  [bold red]💥 错误:[/bold red] {error}")


# ── CLI 命令 ────────────────────────────────────────────

@click.group()
@click.version_option(version="0.1.0")
def cli():
    """🐴 Harness Agent — 驾驭工程实践"""
    pass


@cli.command()
@click.argument("prompt")
@click.option(
    "--project", "-p",
    default=".",
    type=click.Path(exists=True),
    help="项目工作目录",
)
def run(prompt: str, project: str):
    """执行一个开发任务

    示例: harness run "创建一个 hello_world.py"
    """
    project_dir = str(Path(project).resolve())

    console.print(
        Panel(
            f"[bold cyan]任务:[/] {prompt}\n[bold cyan]项目:[/] {project_dir}",
            title="🐴 Harness Agent",
            border_style="cyan",
        )
    )

    asyncio.run(_run_streaming(prompt, project_dir))


async def _run_streaming(prompt: str, project_dir: str):
    """核心流式渲染循环"""
    orchestrator = HarnessOrchestrator(project_dir=project_dir)
    renderer = TerminalRenderer()

    console.print("\n[dim]Agent 正在工作...[/dim]\n")

    async for event in orchestrator.stream(prompt):
        renderer.handle_event(event)

    # 渲染收尾
    console.print("\n")
    if renderer.tool_count > 0:
        console.print(
            f"[dim]共执行了 {renderer.tool_count} 次工具调用[/dim]"
        )
    console.print("[bold green]✅ 任务完成[/bold green]\n")


@cli.command()
def version():
    """显示版本信息"""
    console.print("[bold]Harness Agent[/bold] v0.1.0")
    console.print("[dim]驾驭工程实践 — Powered by Claude Agent SDK[/dim]")


if __name__ == "__main__":
    cli()
