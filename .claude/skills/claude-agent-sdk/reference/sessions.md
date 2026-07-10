# 会话持久化与管理（Phase 2 P2 挂起池）

## `SessionStore` Protocol

> 源码: `types.py:1370`

```python
class SessionStore(Protocol):
    async def append(self, key, entries: list[SessionStoreEntry]) -> None: ...      # 必需
    async def load(self, key) -> list[SessionStoreEntry] | None: ...                # 必需
    async def list_sessions(self, project_key) -> list[SessionStoreListEntry]: ...  # 可选
    async def list_session_summaries(self, project_key) -> list[SessionSummaryEntry]: ...  # 可选
    async def delete(self, key) -> None: ...                                        # 可选
    async def list_subkeys(self, key) -> list[str]: ...                             # 可选
```

- 子进程仍写本地磁盘（`CLAUDE_CONFIG_DIR=/tmp` 可走临时副本），adapter 收到**副**本。
- `append` 在子进程本地写成功**之后**调用--本地耐久性已保证。
- SDK 提供 `InMemorySessionStore`（`_internal/session_store.py`）。

## 模块级会话操作函数（`_internal/session_mutations.py`）

| 函数 | 签名 | 说明 |
|------|------|------|
| `fork_session` | `(session_id, directory=None, up_to_message_id=None, title=None) -> ForkSessionResult` | 分叉到新 session（fresh UUID），可指定分叉点 |
| `tag_session` | `(session_id, tag, directory=None) -> None` | 打标签；`tag=None` 清除 |
| `rename_session` | `(session_id, new_title, directory=None) -> None` | 重命名 |
| `delete_session` | `(session_id, directory=None) -> None` | 硬删除 JSONL + 子 agent 转录 |

每个都有 `*_via_store` async 变体（走 `SessionStore`）。`ForkSessionResult.session_id: str`（`session_mutations.py:233`）。

## 会话列举与读取（`_internal/sessions.py`）

`list_sessions(directory)` / `get_session_info(session_id)` / `get_session_messages(session_id)` / `list_subagents(session_id)` / `get_subagent_messages(...)`，及 `*_from_store` async 变体。返回 `SDKSessionInfo`（`types.py:1496`）/ `SessionMessage`（`types.py:1531`）。

## 星射线落地

挂起池（Phase 2 P2）: `core/pool.py` 用 SQLite 索引 + SDK `session_store`。挂起的 L2 session 通过 `fork_session(up_to_message_id=...)` 从检查点分叉并行尝试，互不污染；`tag_session` 标记挂起状态。
