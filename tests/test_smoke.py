"""冒烟测试：SDK 连通性 + Block 类型探测 + Prompt Caching 验证"""

import pytest


# ─── 测试 1：基础导入 ───
@pytest.mark.asyncio
async def test_sdk_import():
    """验证 SDK 核心类型可正确导入"""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk import AssistantMessage, TextBlock

    options = ClaudeAgentOptions(max_turns=1)
    assert options is not None


# ─── 测试 2：基础调用 ───
@pytest.mark.asyncio
async def test_sdk_simple_query():
    """验证 SDK 可以完成最简单的一次请求"""
    from claude_agent_sdk import query, ClaudeAgentOptions

    messages = []
    async for msg in query(
        prompt="只回复两个字: ok",
        options=ClaudeAgentOptions(
            model="claude-opus-4-7",
            max_turns=1
        ),
    ):
        messages.append(msg)
    assert len(messages) > 0


# ─── 测试 3：Message/Block 类型探测 ───
@pytest.mark.asyncio
async def test_sdk_message_types():
    """探测 SDK 返回的 Message 和 Block 类型

    这个测试的目的不是断言固定结构，
    而是打印出实际的类型名，为 BaseAgent 的事件分类提供依据。
    """
    from claude_agent_sdk import query, ClaudeAgentOptions

    block_types_seen = set()
    message_types_seen = set()

    async for msg in query(
        prompt="列出当前目录的文件",
        options=ClaudeAgentOptions(
            model="claude-opus-4-7",
            max_turns=2,
            allowed_tools=["Bash"],
        ),
    ):
        message_types_seen.add(type(msg).__name__)
        if hasattr(msg, "content"):
            for block in msg.content:
                block_types_seen.add(type(block).__name__)

    print(f"\n[探测结果] Message 类型: {message_types_seen}")
    print(f"[探测结果] Block 类型:   {block_types_seen}")

    # 至少应该看到 AssistantMessage
    assert "AssistantMessage" in message_types_seen


# ─── 测试 4：Prompt Caching 探测 ───
@pytest.mark.asyncio
async def test_prompt_caching_support():
    """探测 claude-agent-sdk 是否支持 Prompt Caching

    方法：用相同的 system_prompt 连续发送两次请求，
    观察 SDK 是否暴露了 cache 相关的 usage 信息。

    ⚠️ 这是一个探测性测试，不是断言性测试。
    结果需要人工判读，决定后续 Caching 策略。
    """
    from claude_agent_sdk import query, ClaudeAgentOptions

    # 故意加长以触发 Caching 阈值（>1024 tokens）
    system = "你是一个测试助手。请简短回复。\n" * 100

    options = ClaudeAgentOptions(
        model="claude-opus-4-7",
        system_prompt=system,
        max_turns=1,
    )

    # 第一次请求
    msgs_1 = []
    async for msg in query(prompt="说1", options=options):
        msgs_1.append(msg)
        # 探测 usage 属性
        if hasattr(msg, "usage"):
            print(f"\n[Caching 探测 - 请求1] usage: {msg.usage}")
        if hasattr(msg, "model_dump"):
            try:
                dump = msg.model_dump()
                if "usage" in dump:
                    print(f"[Caching 探测 - 请求1] dump.usage: {dump['usage']}")
            except Exception:
                pass

    # 第二次请求（相同 system_prompt）
    msgs_2 = []
    async for msg in query(prompt="说2", options=options):
        msgs_2.append(msg)
        if hasattr(msg, "usage"):
            print(f"\n[Caching 探测 - 请求2] usage: {msg.usage}")

    print("\n[Caching 结论] 请检查上方输出:")
    print("  - 如果看到 cache_creation_input_tokens → SDK 原生支持 ✅")
    print("  - 如果没有任何 cache 字段 → 需要手动注入 cache_control ⚠️")
    print("  - 如果 SDK 不暴露 usage → 通过 API Dashboard 验证 🔍")

    assert len(msgs_1) > 0


# ─── 测试 5：harness_agent 包自身可导入 ───
def test_package_import():
    """验证 harness_agent 包可以正确导入"""
    from harness_agent import __version__
    assert __version__ == "0.1.0"


def test_config_import():
    """验证配置模块可以正确导入"""
    from harness_agent.config import HarnessConfig
    config = HarnessConfig(project_dir=".")
    assert config.max_turns == 15


def test_state_import():
    """验证状态模块可以正确导入"""
    from harness_agent.core.state import HarnessState
    # TypedDict 是类型，验证它是 dict 的子类
    assert issubclass(HarnessState, dict)
