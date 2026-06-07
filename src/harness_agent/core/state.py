"""LangGraph 状态定义"""

from typing import TypedDict, Annotated
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class HarnessState(TypedDict):
    """LangGraph 全局状态

    ⚠️ 关于"脑裂"的设计决策（v3 明确）：
    ─────────────────────────────────────────
    messages 字段仅作为 Agent 执行结果的「归档记录」。
    Agent 内部的 ReAct 循环（思考→工具调用→观察→再思考）
    完全由 claude-agent-sdk 内部的状态机闭环管理。

    LangGraph State 和 SDK 内部状态是两个独立的世界。
    在 Phase 1 中，我们不试图统一它们。
    在 Phase 2 的 Harness 重试循环中，我们通过
    「将上一轮的归档摘要注入到下一轮的 prompt」来桥接。
    ─────────────────────────────────────────

    Attributes:
        messages: 对话归档（add_messages reducer 自动追加）
        task:     用户原始任务描述
        project_dir: 项目工作目录
    """

    messages: Annotated[list[BaseMessage], add_messages]
    task: str
    project_dir: str
