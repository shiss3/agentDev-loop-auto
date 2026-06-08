"""斜杠命令测试"""

from harness_agent.chat.commands import get_command, all_commands


def test_builtin_commands_registered():
    cmds = all_commands()
    assert "help" in cmds
    assert "exit" in cmds
    assert "quit" in cmds
    assert "clear" in cmds
    assert "context" in cmds
    assert "stats" in cmds
    assert "project" in cmds
    assert "model" in cmds


def test_get_unknown_command():
    assert get_command("nonexistent") is None


def test_get_known_command():
    cmd = get_command("help")
    assert cmd is not None
    assert cmd.name == "help"
    assert cmd.handler is not None


def test_all_commands_have_handlers():
    for name, cmd in all_commands().items():
        assert cmd.name == name
        assert callable(cmd.handler)
        assert cmd.description  # 非空描述
