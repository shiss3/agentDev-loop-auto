"""斜杠命令注册与分发

用户在 REPL 中输入 / 开头的命令时，
由此模块解析并执行，不发送给 Agent。

Phase 2.5 (TUI 版):
  命令输出通过 ChatCLI.renderer.render_command_result() → ContentBuffer 渲染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop_agent.chat.repl import ChatCLI


@dataclass
class SlashCommand:
    """斜杠命令定义"""

    name: str
    description: str
    handler: Callable  # async def handler(cli: ChatCLI, args: str) -> None
    usage: str = ""


# ── 命令注册表 ──

_COMMANDS: dict[str, SlashCommand] = {}


def slash_command(name: str, description: str, usage: str = ""):
    """装饰器：注册斜杠命令"""

    def decorator(func):
        _COMMANDS[name] = SlashCommand(
            name=name,
            description=description,
            handler=func,
            usage=usage,
        )
        return func

    return decorator


def get_command(name: str) -> SlashCommand | None:
    return _COMMANDS.get(name)


def all_commands() -> dict[str, SlashCommand]:
    return _COMMANDS.copy()


# ── 内置命令实现 ──


@slash_command("help", "显示所有可用命令")
async def cmd_help(cli: "ChatCLI", args: str) -> None:
    lines = ["可用命令:"]
    for name, cmd in sorted(_COMMANDS.items()):
        usage_str = f" {cmd.usage}" if cmd.usage else ""
        lines.append(f"  /{name}{usage_str}  — {cmd.description}")
    cli.renderer.render_command_result("\n".join(lines))


@slash_command("exit", "退出聊天")
async def cmd_exit(cli: "ChatCLI", args: str) -> None:
    cli.request_exit()


@slash_command("quit", "退出聊天（同 /exit）")
async def cmd_quit(cli: "ChatCLI", args: str) -> None:
    cli.request_exit()


@slash_command("clear", "清空当前会话，重新开始")
async def cmd_clear(cli: "ChatCLI", args: str) -> None:
    await cli.reset_session()
    cli.renderer.render_command_result("会话已清空")


@slash_command("context", "显示当前共享上下文摘要", usage="[show]")
async def cmd_context(cli: "ChatCLI", args: str) -> None:
    summary = cli.session.context_provider.get_context_summary()
    cli.renderer.render_command_result(summary)


@slash_command("stats", "显示当前会话统计")
async def cmd_stats(cli: "ChatCLI", args: str) -> None:
    stats = cli.session.stats.to_dict()
    lines = [
        f"  对话轮次:    {stats['turn_count']}",
        f"  工具调用:    {stats['total_tool_calls']}",
        f"  输入 Token:  {stats['total_input_tokens']}",
        f"  输出 Token:  {stats['total_output_tokens']}",
    ]
    cli.renderer.render_command_result("\n".join(lines))


@slash_command("project", "显示或切换项目目录", usage="[path]")
async def cmd_project(cli: "ChatCLI", args: str) -> None:
    if args.strip():
        new_dir = args.strip()
        cli.renderer.render_command_result(
            f"切换项目到 {new_dir}，会话将重建..."
        )
        await cli.switch_project(new_dir)
    else:
        cli.renderer.render_command_result(
            f"当前项目: {cli.session.project_dir}"
        )


@slash_command("model", "显示或切换模型", usage="[model_name]")
async def cmd_model(cli: "ChatCLI", args: str) -> None:
    if args.strip():
        cli.renderer.render_command_result(
            f"切换模型到 {args.strip()}，会话将重建..."
        )
        await cli.switch_model(args.strip())
    else:
        current = cli.session.model or "default"
        cli.renderer.render_command_result(f"当前模型: {current}")


@slash_command("auto", "切换到自动模式（解析需求后判轨，默认）")
async def cmd_auto(cli: "ChatCLI", args: str) -> None:
    cli.mode = "auto"
    cli.renderer.render_command_result(
        "已切换到自动模式：解析需求后判轨（interactive/delivery）"
    )


@slash_command("semi", "切换到半自动模式（输入直通交互轨，常驻理解+propose_plan+采纳走交付）")
async def cmd_semi(cli: "ChatCLI", args: str) -> None:
    cli.mode = "semi"
    cli.renderer.render_command_result(
        "已切换到半自动模式：输入直通交互轨，常驻执行体理解需求并生成方案；"
        "回复“采用方案”灌队列走交付轨，或直接对话在本轨执行"
    )
