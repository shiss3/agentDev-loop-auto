"""LangGraph 编排引擎 — Phase 1 极简直线图"""

from langgraph.graph import StateGraph, END
from harness_agent.core.state import HarnessState
from harness_agent.core.agents.base_agent import BaseAgent


def build_graph() -> StateGraph:
    """构建 Phase 1 的极简状态图

    START ──→ default_agent ──→ END

    没有 Router，没有条件分支。
    只验证：用户任务 → Agent 执行 → 结果回传。
    """
    # 初始化 Agent 实例
    default_agent = BaseAgent(name="default")

    # 构建状态图
    graph = StateGraph(HarnessState)

    # 唯一节点：default_agent
    graph.add_node("default_agent", default_agent)

    # 直线连接
    graph.set_entry_point("default_agent")
    graph.add_edge("default_agent", END)

    return graph


class HarnessOrchestrator:
    """编排器封装 — 提供编译后的图实例"""

    def __init__(self, project_dir: str = "."):
        self.project_dir = project_dir
        self.graph = build_graph().compile()

    async def run(self, task: str) -> dict:
        """同步执行（非流式），用于测试"""
        initial_state: HarnessState = {
            "messages": [],
            "task": task,
            "project_dir": self.project_dir,
        }
        result = await self.graph.ainvoke(initial_state)
        return result

    async def stream(self, task: str):
        """流式执行，yield astream_events 事件

        供 CLI 层消费，实现终端实时渲染。
        """
        initial_state: HarnessState = {
            "messages": [],
            "task": task,
            "project_dir": self.project_dir,
        }
        async for event in self.graph.astream_events(
            initial_state, version="v2"
        ):
            yield event
