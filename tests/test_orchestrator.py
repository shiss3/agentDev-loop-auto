"""Orchestrator 单元测试"""

import pytest
from harness_agent.core.orchestrator import HarnessOrchestrator


@pytest.mark.asyncio
async def test_graph_compiles():
    """验证状态图可以正确编译"""
    orch = HarnessOrchestrator()
    assert orch.graph is not None


# 这里的 test_graph_runs 会触发到底层的 SDK 调用，如果使用中转站可能会失败，
# 所以我们这里仅测试编译通过即可。如果您有正确的官方 key，可以解除注释进行测试。
# @pytest.mark.asyncio
# async def test_graph_runs():
#     """验证状态图可以端到端执行"""
#     orch = HarnessOrchestrator(project_dir=".")
#     result = await orch.run("回复 'hello harness'")
#     assert "messages" in result
#     assert len(result["messages"]) > 0
