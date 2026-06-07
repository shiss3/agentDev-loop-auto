"""Agent 基类 — 纯函数，全类型事件发射，无 IO 副作用"""

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)
from langchain_core.messages import AIMessage
from langchain_core.callbacks import adispatch_custom_event

from harness_agent.core.state import HarnessState


_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。"""


class BaseAgent:
    """LangGraph 节点 — 纯函数语义

    核心职责：
    1. 调用 claude-agent-sdk 执行任务
    2. 对 SDK 返回的每种 Block 类型发射不同的自定义事件
    3. 组装完整结果，返回 State update

    绝不做的事：
    - ❌ print() / sys.stdout.write()
    - ❌ 直接操作终端
    - ❌ 管理对话历史（SDK 内部闭环）
    """

    def __init__(
        self,
        name: str = "default",
        system_prompt: str = _BASE_SYSTEM_PROMPT,
        allowed_tools: list[str] | None = None,
        max_turns: int = 15,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]
        self.max_turns = max_turns

    def _build_options(self, project_dir: str) -> ClaudeAgentOptions:
        """构建 SDK 配置"""
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            cwd=project_dir,
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            permission_mode="acceptEdits",
        )

    async def __call__(self, state: HarnessState) -> dict:
        """LangGraph 节点入口

        ⚠️ 关于状态管理的"脑裂"：
        我们只从 state 中取 task 和 project_dir。
        state["messages"] 中的历史记录不会被喂给 SDK。
        SDK 内部有自己的 ReAct 状态机，负责工具调用循环。
        LangGraph 的 messages 仅用于归档最终结果。
        """
        task = state["task"]
        project_dir = state.get("project_dir", ".")
        options = self._build_options(project_dir)

        collected_texts: list[str] = []
        tool_calls: list[dict] = []

        # 捕获可能由于 SDK 初始化失败或 Key 不正确导致的异常
        try:
            async for message in query(prompt=task, options=options):

                # ── AssistantMessage: 模型的思考和工具调用意图 ──
                if isinstance(message, AssistantMessage):
                    for block in message.content:

                        # 文本块：模型在"说话"
                        if isinstance(block, TextBlock):
                            collected_texts.append(block.text)
                            await adispatch_custom_event(
                                "agent_text",
                                {
                                    "agent": self.name,
                                    "text": block.text,
                                },
                            )

                        # 工具调用块：模型在"动手"
                        elif isinstance(block, ToolUseBlock):
                            tool_info = {
                                "agent": self.name,
                                "tool_name": block.name,
                                "tool_id": block.id,
                                "tool_input": _summarize_tool_input(block.input),
                            }
                            tool_calls.append(tool_info)
                            await adispatch_custom_event(
                                "agent_tool",
                                tool_info,
                            )

                # ── ResultMessage: 任务整体执行结果 ──
                elif isinstance(message, ResultMessage):
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", [])
                        err_text = "\\n".join(errors) if errors else "API 无法连接或其它错误"
                        await adispatch_custom_event(
                            "agent_error",
                            {
                                "agent": self.name,
                                "error": err_text[:500],
                            },
                        )
                    else:
                        res = getattr(message, "result", "")
                        await adispatch_custom_event(
                            "agent_result",
                            {
                                "agent": self.name,
                                "content": str(res)[:500] if res else "✅ 任务顺利完成",
                                "is_error": False,
                            },
                        )

        except Exception as e:
            # 向外发射错误事件，防止程序静默崩溃
            await adispatch_custom_event(
                "agent_error",
                {
                    "agent": self.name,
                    "error": f"SDK 内部异常: {str(e)}",
                },
            )
            collected_texts.append(f"\n[❌ 致命错误: SDK 内部异常] {str(e)}")

        # ── 组装最终归档结果 ──
        summary_parts = []
        if collected_texts:
            summary_parts.append("\n".join(collected_texts))
        if tool_calls:
            tool_summary = ", ".join(
                f"{t['tool_name']}({t['tool_input']})" for t in tool_calls
            )
            summary_parts.append(f"\n[工具调用记录] {tool_summary}")

        response_text = (
            "\n".join(summary_parts) if summary_parts
            else "[Agent 执行完毕，无文本输出]"
        )

        return {
            "messages": [AIMessage(content=response_text, name=self.name)]
        }


def _summarize_tool_input(tool_input: dict) -> str:
    """将工具输入摘要化，避免超长内容污染日志"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:100]

    # 常见的工具输入字段
    if "command" in tool_input:
        return tool_input["command"][:100]
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "content" in tool_input:
        return f"[{len(tool_input['content'])} chars]"
    return str(tool_input)[:100]
