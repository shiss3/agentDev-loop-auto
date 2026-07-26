"""编排器 — 纯 async 封装，零框架依赖

替代 Phase 1 的 LangGraph StateGraph。
直接调用 base_agent.run_agent() async generator，
产出与 astream_events v2 兼容的事件 dict（保证 cli.py 不改）。
"""

from autoloop_agent.core.agents.base_agent import run_agent


class AutoLoopOrchestrator:
    """编排器封装 — 纯 async Python，无 LangGraph 依赖

    保持与 Phase 1 完全相同的公开接口：
        orchestrator.stream(task) → AsyncIterator[dict]
        orchestrator.run(task)     → dict

    cli.py / TerminalRenderer 不需要任何修改。
    """

    def __init__(self, project_dir: str = ".", model: str | None = None):
        self.project_dir = project_dir
        self.model = model

    async def run(self, task: str) -> dict:
        """同步执行（非流式），用于测试

        收集所有事件，返回与旧 StateGraph.ainvoke 兼容的格式。
        """
        messages = []
        async for _event in self.stream(task):
            pass  # 收集完成即可，事件本身不纳入返回值
        return {"messages": messages, "task": task, "project_dir": self.project_dir}

    async def stream(self, task: str):
        """流式执行，yield 事件 dict

        格式与 LangGraph astream_events v2 完全兼容：
            {"event": "on_custom_event", "name": "...", "data": {...}}

        cli.py 的 TerminalRenderer.handle_event() 消费此格式。
        """
        async for event in run_agent(
            task=task,
            project_dir=self.project_dir,
            model=self.model,
        ):
            yield event
