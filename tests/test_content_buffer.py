"""ContentBuffer 单元测试"""

from prompt_toolkit.formatted_text import FormattedText

from autoloop_agent.chat.content_buffer import ContentBuffer


class TestContentBufferBasics:
    """基础内容操作"""

    def test_empty_buffer(self):
        buf = ContentBuffer()
        assert buf.line_count == 0
        ft = buf.get_formatted_text()
        assert ft == FormattedText([("", "")])

    def test_append_text(self):
        buf = ContentBuffer()
        buf.append_text("hello world")
        assert buf.line_count == 1
        ft = buf.get_formatted_text()
        # 应包含 "hello world" 文本
        text_content = "".join(t for _, t in ft)
        assert "hello world" in text_content

    def test_append_user_input(self):
        buf = ContentBuffer()
        buf.append_user_input("analyze user.py")
        assert buf.line_count == 1
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "You > " in text_content
        assert "analyze user.py" in text_content

    def test_append_tool_line(self):
        buf = ContentBuffer()
        buf.append_tool_line("📖", 1, "Read", "user.py", status="ok")
        assert buf.line_count == 1
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "Read" in text_content
        assert "user.py" in text_content
        assert "1" in text_content

    def test_append_error(self):
        buf = ContentBuffer()
        buf.append_error("something broke")
        assert buf.line_count == 1
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "something broke" in text_content

    def test_append_blank_line(self):
        buf = ContentBuffer()
        buf.append_text("line 1")
        buf.append_blank_line()
        buf.append_text("line 3")
        assert buf.line_count == 3

    def test_append_plain(self):
        buf = ContentBuffer()
        buf.append_plain("command result")
        assert buf.line_count == 1
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "command result" in text_content

    def test_clear(self):
        buf = ContentBuffer()
        buf.append_text("content")
        buf.append_text("more")
        assert buf.line_count == 2
        buf.clear()
        assert buf.line_count == 0

    def test_multiple_lines_joined_with_newline(self):
        buf = ContentBuffer()
        buf.append_text("line 1")
        buf.append_text("line 2")
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "line 1" in text_content
        assert "line 2" in text_content
        assert "\n" in text_content


class TestContentBufferSpinner:
    """Spinner 管理"""

    def test_spinner_starts_inactive(self):
        buf = ContentBuffer()
        assert buf.spinner_active is False

    def test_start_stop_spinner(self):
        buf = ContentBuffer()
        buf.start_spinner("thinking...")
        assert buf.spinner_active is True

        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "thinking..." in text_content

        buf.stop_spinner()
        assert buf.spinner_active is False

    def test_spinner_text_in_output(self):
        buf = ContentBuffer()
        buf.start_spinner("executing...")
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "executing..." in text_content
        buf.stop_spinner()

    def test_spinner_not_in_output_when_stopped(self):
        buf = ContentBuffer()
        buf.append_text("content")
        buf.start_spinner("thinking...")
        buf.stop_spinner()
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "thinking..." not in text_content

    def test_tick_spinner(self):
        buf = ContentBuffer()
        buf.start_spinner("thinking...")
        # tick 不应报错，frame 应该推进
        initial_frame = buf._spinner_frame
        buf.tick_spinner()
        assert buf._spinner_frame == (initial_frame + 1) % 10
        buf.stop_spinner()

    def test_tick_spinner_does_nothing_when_stopped(self):
        buf = ContentBuffer()
        buf.tick_spinner()  # 不应报错

    def test_stop_spinner_when_not_started(self):
        buf = ContentBuffer()
        buf.stop_spinner()  # 不应报错

    def test_start_spinner_replaces_previous(self):
        buf = ContentBuffer()
        buf.start_spinner("first")
        buf.start_spinner("second")
        assert buf.spinner_active is True
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "second" in text_content
        assert "first" not in text_content


class TestContentBufferOnChange:
    """变更回调"""

    def test_on_change_called_on_append(self):
        call_count = 0

        def on_change():
            nonlocal call_count
            call_count += 1

        buf = ContentBuffer()
        buf.set_on_change(on_change)
        buf.append_text("test")
        assert call_count == 1

    def test_on_change_called_on_spinner(self):
        events: list[str] = []

        def on_change():
            events.append("change")

        buf = ContentBuffer()
        buf.set_on_change(on_change)
        buf.start_spinner()
        assert len(events) == 1
        buf.tick_spinner()
        assert len(events) == 2
        buf.stop_spinner()
        assert len(events) == 3

    def test_on_change_called_on_clear(self):
        events: list[str] = []

        def on_change():
            events.append("change")

        buf = ContentBuffer()
        buf.set_on_change(on_change)
        buf.append_text("x")
        buf.clear()
        assert len(events) == 2


class TestContentBufferToolStatus:
    """工具行状态样式"""

    def test_tool_ok_status(self):
        buf = ContentBuffer()
        buf.append_tool_line("📖", 1, "Read", "file.py", status="ok")
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "✓" in text_content

    def test_tool_fail_status(self):
        buf = ContentBuffer()
        buf.append_tool_line("⚡", 2, "Bash", "cmd", status="fail")
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "✗" in text_content

    def test_tool_running_status(self):
        buf = ContentBuffer()
        buf.append_tool_line("✏️", 3, "Write", "out.py", status="running")
        ft = buf.get_formatted_text()
        text_content = "".join(t for _, t in ft)
        assert "..." in text_content
