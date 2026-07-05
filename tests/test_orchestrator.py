"""Orchestrator 单元测试 — 纯 async 版"""

import pytest
from harness_agent.core.orchestrator import HarnessOrchestrator


@pytest.mark.asyncio
async def test_orchestrator_creates():
    """验证编排器可以正确创建"""
    orch = HarnessOrchestrator()
    assert orch is not None
    assert orch.project_dir is not None


@pytest.mark.asyncio
async def test_run_agent_imports():
    """验证纯 async agent 函数可正确导入"""
    from harness_agent.core.agents.base_agent import run_agent
    assert run_agent is not None
