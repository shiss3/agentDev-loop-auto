"""极简 Agent 执行函数 — 纯 async generator，零框架依赖

职责：
1. 调用 claude-agent-sdk 执行任务
2. 对 SDK 返回的每种 Block 类型 yield 不同的事件 dict
3. 完全不依赖 LangGraph / langchain-core

产出的事件格式（与 cli.py TerminalRenderer 兼容）：
    {"event": "on_custom_event", "name": "<type>", "data": {...}}

绝不做的事：
- ❌ print() / sys.stdout.write()
- ❌ 直接操作终端
- ❌ 管理对话历史（SDK 内部闭环）
"""

import time

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)


# 基础系统提示词（当不使用 Claude Code preset 时使用）
_BASE_SYSTEM_PROMPT = """你是 AutoLoop 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。

## 路径说明
- 所有相对路径（如 docs/1.md）都基于当前工作目录（cwd）解析。
- 如果用户问"项目中某个文件"，先用 Glob 或 Bash 确认项目根目录结构，
  再按 cwd 定位文件。不要跨 cwd 范围去寻找项目外的路径。"""


async def run_agent(
    task: str,
    project_dir: str = ".",
    *,
    name: str = "default",
    system_prompt: str = _BASE_SYSTEM_PROMPT,
    allowed_tools: list[str] | None = None,
    max_turns: int = 15,
    model: str | None = None,
):
    """纯 async generator：执行 SDK 任务并 yield 流式事件

    Args:
        task:         用户任务描述
        project_dir:  项目工作目录
        name:         Agent 名称
        system_prompt: 系统提示词
        allowed_tools: 工具白名单
        max_turns:    最大轮次
        model:        模型名称

    Yields:
        dict: 格式为 {"event": "on_custom_event", "name": "<type>", "data": {...}}
              保证与 cli.py TerminalRenderer 兼容。
    """
    tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]

    _system_prompt = (
        f"{system_prompt}\n\n"
        f"## 当前工作目录\n"
        f"cwd = {project_dir}\n"
        f"所有相对路径都基于此目录解析。"
    )

    options = ClaudeAgentOptions(
        system_prompt=_system_prompt,
        cwd=project_dir,
        allowed_tools=tools,
        max_turns=max_turns,
        permission_mode="acceptEdits",
        model=model,
    )

    collected_texts: list[str] = []
    tool_calls: list[dict] = []
    model_info: dict | None = None
    start_time = time.monotonic()

    try:
        async for message in query(prompt=task, options=options):

            # ── AssistantMessage: 模型的思考和工具调用意图 ──
            if isinstance(message, AssistantMessage):
                for block in message.content:

                    # 文本块：模型在"说话"
                    if isinstance(block, TextBlock):
                        collected_texts.append(block.text)
                        yield {
                            "event": "on_custom_event",
                            "name": "agent_text",
                            "data": {
                                "agent": name,
                                "text": block.text,
                            },
                        }

                    # 工具调用块：模型在"动手"
                    elif isinstance(block, ToolUseBlock):
                        tool_info = {
                            "agent": name,
                            "tool_name": block.name,
                            "tool_id": block.id,
                            "tool_input": _summarize_tool_input(block.input),
                        }
                        tool_calls.append(tool_info)
                        yield {
                            "event": "on_custom_event",
                            "name": "agent_tool",
                            "data": tool_info,
                        }

            # ── ResultMessage: 任务整体执行结果 ──
            elif isinstance(message, ResultMessage):
                if msg_model_usage := getattr(message, "model_usage", None):
                    model_info = dict(msg_model_usage)

                if getattr(message, "is_error", False):
                    errors = getattr(message, "errors", [])
                    err_text = "\n".join(errors) if errors else "API 无法连接或其它错误"
                    yield {
                        "event": "on_custom_event",
                        "name": "agent_error",
                        "data": {
                            "agent": name,
                            "error": err_text[:500],
                        },
                    }
                else:
                    duration_s = round(time.monotonic() - start_time, 1)
                    res = getattr(message, "result", "")
                    yield {
                        "event": "on_custom_event",
                        "name": "agent_result",
                        "data": {
                            "agent": name,
                            "content": str(res)[:500] if res else "✅ 任务顺利完成",
                            "is_error": False,
                            "model_usage": model_info,
                            "duration_s": duration_s,
                        },
                    }

    except Exception as exc:
        yield {
            "event": "on_custom_event",
            "name": "agent_error",
            "data": {
                "agent": name,
                "error": f"SDK 内部异常: {exc}",
            },
        }
        collected_texts.append(f"\n[❌ 致命错误: SDK 内部异常] {exc}")


def _summarize_tool_input(tool_input: dict) -> str:
    """将工具输入摘要化，避免超长内容污染日志"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:100]

    if "command" in tool_input:
        return tool_input["command"][:100]
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "content" in tool_input:
        return f"[{len(tool_input['content'])} chars]"
    return str(tool_input)[:100]
