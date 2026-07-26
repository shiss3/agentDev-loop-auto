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
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.panel import Panel

from autoloop_agent.core.orchestrator import AutoLoopOrchestrator

# 从 CWD 向上加载 .env(打包安装后从用户项目目录读;开发模式从仓库根读)
load_dotenv(find_dotenv(usecwd=True))

console = Console()


# ── 事件渲染器 ──────────────────────────────────────────

class TerminalRenderer:
    """将 astream_events 中的自定义事件渲染到终端"""

    def __init__(self):
        self.tool_count = 0
        self.model_usage: dict | None = None
        self.duration_s: float = 0.0

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
            # 捕获实际模型信息和耗时
            if data.get("model_usage"):
                self.model_usage = data["model_usage"]
            if data.get("duration_s"):
                self.duration_s = data["duration_s"]
            if is_error:
                console.print(f"  [red]❌ 失败:[/red] [dim]{content}[/dim]")
            else:
                console.print("  [green]✅ 完成[/green]")

        elif name == "agent_error":
            error = data.get("error", "")[:300]
            console.print(f"\n  [bold red]💥 错误:[/bold red] {error}")


# ── CLI 命令 ────────────────────────────────────────────

@click.group()
@click.version_option()
def cli():
    """🐴 AutoLoop - 自动循环开发"""
    pass


def _detect_project_root(start: Path) -> Path:
    """从 start 目录向上查找项目根目录

    检测信号（按优先级）：
      1. pyproject.toml
      2. .git
      3. package.json
    都找不到则回退到 start 本身。
    """
    markers = ("pyproject.toml", ".git", "package.json")
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        for marker in markers:
            if (parent / marker).exists():
                return parent
    return cur


@cli.command()
@click.argument("prompt")
@click.option(
    "--project", "-p",
    default=None,
    type=click.Path(exists=True),
    help="项目工作目录（默认自动探测最近的项目根：pyproject.toml / .git / package.json）",
)
@click.option(
    "--model", "-m",
    default=None,
    help="模型名称，如 glm-5.1（默认走全局 settings.json 配置）",
)
def run(prompt: str, project: str | None, model: str):
    """执行一个开发任务

    示例: autoloop run "创建一个 hello_world.py"
    """
    # 未指定 project 时自动探测项目根
    if project is None:
        project_dir = str(_detect_project_root(Path.cwd()))
    else:
        project_dir = str(Path(project).resolve())

    console.print(
        Panel(
            f"[bold cyan]任务:[/] {prompt}\n[bold cyan]项目:[/] {project_dir}"
            + (f"\n[bold cyan]模型:[/] {model}" if model else ""),
            title="🐴 AutoLoop",
            border_style="cyan",
        )
    )

    asyncio.run(_run_streaming(prompt, project_dir, model))


async def _run_streaming(prompt: str, project_dir: str, model: str | None = None):
    """核心流式渲染循环"""
    orchestrator = AutoLoopOrchestrator(project_dir=project_dir, model=model)
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
    # 显示实际模型
    if renderer.model_usage:
        for model_name, usage in renderer.model_usage.items():
            cost = usage.get("costUSD", 0)
            in_t = usage.get("inputTokens", 0)
            out_t = usage.get("outputTokens", 0)
            duration = renderer.duration_s
            console.print(
                f"[dim]模型: {model_name} | "
                f"tokens: {in_t}in/{out_t}out | "
                f"费用: ${cost:.4f} | "
                f"耗时: {duration}s[/dim]"
            )
    console.print("[bold green]✅ 任务完成[/bold green]\n")


@cli.command()
@click.option(
    "--project", "-p",
    default=None,
    type=click.Path(exists=True),
    help="项目工作目录（默认自动探测最近的项目根：pyproject.toml / .git / package.json）",
)
@click.option(
    "--model", "-m",
    default=None,
    help="模型名称，如 glm-5.1（默认走全局 settings.json 配置）",
)
@click.option(
    "--resume", "-r",
    default=None,
    help="恢复指定的会话 ID",
)
@click.option(
    "--continue", "-c",
    "continue_conversation",
    is_flag=True,
    default=False,
    help="恢复最近一次会话",
)
@click.option(
    "--undo",
    is_flag=True,
    default=False,
    help="开启检查点模式（支持 /undo 回滚，但禁用会话恢复功能）",
)
def chat(project: str | None, model: str | None, resume: str | None, continue_conversation: bool, undo: bool):
    """启动交互式聊天模式（多轮对话 REPL）

    示例:
        autoloop chat                       # 新会话（显示历史选择）
        autoloop chat -p ./my-app          # 指定项目
        autoloop chat -m claude-sonnet-4-6 # 指定模型
        autoloop chat --continue           # 恢复最近会话
        autoloop chat --resume <session_id> # 恢复指定会话
        autoloop chat --undo               # 检查点模式（支持 /undo）

    注意：--undo 和 --continue/--resume 互斥
    """
    from autoloop_agent.chat.repl import ChatCLI

    # 检查互斥
    if undo and (continue_conversation or resume):
        click.echo("错误：--undo 不能与 --continue 或 --resume 同时使用", err=True)
        return

    # 未指定 project 时自动探测项目根
    if project is None:
        project_dir = str(_detect_project_root(Path.cwd()))
    else:
        project_dir = str(Path(project).resolve())

    chat_cli = ChatCLI(
        project_dir=project_dir,
        model=model,
        resume_session_id=resume if not undo else None,
        continue_conversation=continue_conversation if not undo else False,
        enable_undo=undo,
    )
    asyncio.run(chat_cli.run())


@cli.command()
def version():
    """显示版本信息"""
    from autoloop_agent import __version__
    console.print(f"[bold]AutoLoop[/bold] v{__version__}")
    console.print("[dim]自动循环开发 - Powered by Claude Agent SDK[/dim]")


if __name__ == "__main__":
    cli()
