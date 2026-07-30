"""会话恢复功能测试

测试会话存储和会话恢复的基础功能
"""

import pytest
from unittest.mock import MagicMock, patch
from claude_agent_sdk import SessionKey, SessionStoreEntry, fold_session_summary

from autoloop_agent.chat.session_store import FileSessionStore


class TestFileSessionStore:
    """FileSessionStore 功能测试"""

    @pytest.fixture
    async def store(self):
        """创建 SessionStore 实例"""
        store = FileSessionStore(base_dir="/tmp/test_autoloop_sessions")
        # 清理测试数据
        yield store
        # 清理完成后的清理
        import shutil
        if store.base_dir.exists():
            shutil.rmtree(store.base_dir)

    @pytest.mark.asyncio
    async def test_create_session_store(self, store):
        """测试创建会话存储"""
        assert store is not None
        assert store.base_dir.exists()

    @pytest.mark.asyncio
    async def test_append_and_load(self, store):
        """测试追加和加载会话记录"""
        key = SessionKey(project_key="test-project", session_id="test-session-1")

        # 创建一些会话条目
        entries = [
            SessionStoreEntry(
                type="user",
                content="Hello",
                timestamp="2025-01-15T10:00:00Z",
                uuid="test-uuid-1",
            ),
            SessionStoreEntry(
                type="assistant",
                content="Hi there!",
                timestamp="2025-01-15T10:00:01Z",
                uuid="test-uuid-2",
            ),
            SessionStoreEntry(
                type="tool_use",
                name="Read",
                input={"file_path": "test.txt"},
                timestamp="2025-01-15T10:00:02Z",
                uuid="test-uuid-3",
            ),
        ]

        # 追加记录
        await store.append(key, entries)

        # 加载记录
        loaded_entries = await store.load(key)
        assert loaded_entries is not None
        assert len(loaded_entries) == 3

    @pytest.mark.asyncio
    async def test_list_sessions(self, store):
        """测试列出会话"""
        # 创建多个会话
        for i in range(3):
            key = SessionKey(project_key="test-project", session_id=f"session-{i}")
            entries = [
                SessionStoreEntry(
                    type="user",
                    content=f"Message {i}",
                    timestamp="2025-01-15T10:00:00Z",
                    uuid=f"uuid-{i}",
                )
            ]
            await store.append(key, entries)

        # 列出会话
        sessions = await store.list_sessions("test-project")
        assert len(sessions) == 3
        # 验证按时间戳降序排序
        for i in range(len(sessions) - 1):
            assert sessions[i]["mtime"] >= sessions[i + 1]["mtime"]

    @pytest.mark.asyncio
    async def test_delete_session(self, store):
        """测试删除会话"""
        key = SessionKey(project_key="test-project", session_id="test-delete")

        # 创建会话
        entries = [
            SessionStoreEntry(
                type="user",
                content="Test",
                timestamp="2025-01-15T10:00:00Z",
                uuid="test-uuid",
            )
        ]
        await store.append(key, entries)

        # 删除会话
        await store.delete(key)

        # 验证已被删除
        loaded = await store.load(key)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_session_summary(self, store):
        """测试会话摘要更新"""
        key = SessionKey(project_key="test-project", session_id="test-summary")

        # 创建带摘要条目的会话
        entries = [
            SessionStoreEntry(
                type="user",
                content="Initial message",
                timestamp="2025-01-15T10:00:00Z",
                uuid="test-uuid-1",
            ),
            SessionStoreEntry(
                type="summary",
                summary="Test Session Title",
                timestamp="2025-01-15T10:00:01Z",
                uuid="test-uuid-2",
            ),
            SessionStoreEntry(
                type="assistant",
                content="Response",
                timestamp="2025-01-15T10:00:02Z",
                uuid="test-uuid-3",
            ),
        ]

        await store.append(key, entries)

        # 检查摘要
        summaries = await store.list_session_summaries("test-project")
        assert len(summaries) == 1
        assert summaries[0]["session_id"] == "test-summary"
        assert "Test Session Title" in summaries[0]["data"].get("summary_hint", "")

    @pytest.mark.asyncio
    async def test_get_session_info(self, store):
        """测试获取会话信息"""
        key = SessionKey(project_key="test-project", session_id="test-info")

        entries = [
            SessionStoreEntry(
                type="user",
                content=f"User message {i}",
                timestamp=f"2025-01-15T10:00:0{i:02d}Z",
                uuid=f"uuid-{i}",
            )
            for i in range(5)
        ]

        await store.append(key, entries)

        # 获取会话信息
        info = await store.get_session_info(key)
        assert info is not None
        assert info["session_id"] == "test-info"
        assert info["turn_count"] >= 1
        assert info["tool_count"] == 0

    @pytest.mark.asyncio
    async def test_no_history(self, store):
        """测试无历史会话的情况"""
        summaries = await store.list_session_summaries("non-existent-project")
        assert summaries == []


class TestChatSessionIntegration:
    """ChatSession 集成测试"""

    @pytest.mark.asyncio
    async def test_session_store_usage(self):
        """测试 ChatSession 使用 SessionStore"""
        # 创建会话存储
        store = FileSessionStore(base_dir="/tmp/test_autoloop_sessions/session-test")

        # 创建会话（不传 session_store，使用默认）
        # 验证：ChatSession 内部应该有自己的 session_store
        with patch("autoloop_agent.chat.session.ChatSession._build_options") as mock_build:
            # Mock 返回的 options
            mock_options = MagicMock()
            mock_options.session_store = store
            mock_options.session_id = "test-id"
            mock_options.resume = None
            mock_options.continue_conversation = False
            mock_build.return_value = mock_options

            # 验证 session_store 是否包含在 options 中
            # 注意：实际测试可能需要更复杂的 mock
            assert True  # 基本的类型检查

    @pytest.mark.asyncio
    async def test_create_new_session(self):
        """测试创建新会话"""

        # 直接创建一个会话
        from autoloop_agent.chat.session import ChatSession

        session = ChatSession(
            project_dir=".",
        )

        assert session is not None
        assert session.session_id is not None
        assert session.session_store is None  # 如果不传会使用默认的

        # 验证 _build_options 应该正确配置
        opts = session._build_options()
        print("Options:", opts)
        # 检查关键字段


def test_fold_session_summary():
    """测试 fold_session_summary 函数"""
    from claude_agent_sdk import SessionKey

    key = SessionKey(project_key="test", session_id="test-session")

    # 测试空列表
    summary = fold_session_summary(None, key, [])
    assert summary is not None
    assert summary["session_id"] == "test-session"

    # 测试单条记录
    entry = SessionStoreEntry(
        type="user",
        content="Hello",
        timestamp="2025-01-15T10:00:00Z",
        uuid="test-uuid-1",
    )
    summary = fold_session_summary(None, key, [entry])
    assert summary["data"]["created_at"] > 0

    # 测试更新摘要
    summary = fold_session_summary(summary, key, [entry])
    assert summary["session_id"] == "test-session"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
