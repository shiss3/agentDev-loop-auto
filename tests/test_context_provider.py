"""ContextProvider 接口测试"""

from harness_agent.context.provider import (
    ContextProvider,
    DefaultContextProvider,
    SystemPromptType,
)


def test_default_provider_returns_claude_code_preset():
    """默认提供者返回 Claude Code preset"""
    provider = DefaultContextProvider()
    result = provider.build_system_prompt("base prompt", "/project")
    # 返回纯 Claude Code preset，无任何追加
    assert result == {"type": "preset", "preset": "claude_code"}


def test_default_provider_summary():
    provider = DefaultContextProvider()
    assert "Claude Code" in provider.get_context_summary()


def test_default_provider_context_never_changes():
    provider = DefaultContextProvider()
    assert provider.has_context_changed() is False


def test_default_provider_empty_history():
    provider = DefaultContextProvider()
    assert provider.get_history_summary([], [], 0) == ""


def test_default_provider_on_turn_end_is_noop():
    provider = DefaultContextProvider()
    # 不应抛异常
    provider.on_turn_end("prompt", "response", [{"tool_name": "Read"}])


def test_protocol_compliance():
    """验证 DefaultContextProvider 实现了 ContextProvider 协议"""
    provider = DefaultContextProvider()
    assert isinstance(provider, ContextProvider)


def test_custom_provider_duck_typing():
    """自定义实现不需要显式继承，只要方法签名匹配"""

    class MyProvider:
        def build_system_prompt(self, base_prompt: str, project_dir: str) -> SystemPromptType:
            # 自定义返回字符串格式
            return f"[{project_dir}] {base_prompt}"

        def on_turn_end(self, prompt: str, response_summary: str, tool_calls: list) -> None:
            pass

        def has_context_changed(self) -> bool:
            return False

        def get_history_summary(self, texts: list, calls: list, turns: int) -> str:
            return ""

        def get_context_summary(self) -> str:
            return "my context"

    provider = MyProvider()
    assert isinstance(provider, ContextProvider)
    assert "[/app]" in provider.build_system_prompt("hi", "/app")
