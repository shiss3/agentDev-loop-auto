"""SessionStore 存储位置与实现

职责：
1. 初始化 FileSessionStore，服务于聊天应用的会话管理
2. 将 SDK 的 session_store 持久化存储到本地文件系统
3. 提供 list_sessions/get_session 等查询接口

存储结构：
├── ~/.autoloop/sessions/
│   ├── .summary.json          # 每个命名的会话摘要文件
│   └── .transcript.jsonl      # 会话原始对话记录
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    fold_session_summary,
    SessionKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
)
from claude_agent_sdk.types import SessionListSubkeysKey

logger = logging.getLogger(__name__)


class FileSessionStore(SessionStore):
    """文件系统会话存储

    将会话记录持久化到本地 JSON 文件，支持：
    - 读取历史会话列表
    - 加载单个会话的完整记录
    - 检查会话是否存在

    目录结构：
        ~/.autoloop/sessions/
            ├── .summary.json          # 会话摘要（title, created_at, mtime）
            ├── .transcript.jsonl      # 会话原始记录（每行一个 SessionStoreEntry）
            └── .metadata.json         # 会话元数据（可选扩展）
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        """初始化文件存储

        Args:
            base_dir: 会话存储基础目录，默认 ~/.autoloop/sessions
        """
        if base_dir is None:
            base_dir = Path.home() / ".autoloop" / "sessions"

        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓存（用于查询）
        self._cache: dict[str, dict[str, Any]] = {}
        self._summary_cache: dict[str, dict[str, Any]] = {}

    # ── 必需方法 ──

    async def append(
        self,
        key: SessionKey,
        entries: list[SessionStoreEntry],
    ) -> None:
        """追加会话记录

        Args:
            key: 会话键（project_key/session_id）
            entries: 会话条目列表
        """
        keys = self._require_keys(key)
        if keys is None:
            logger.warning(f"Ignoring entry with incomplete key: {key}")
            return
        session_id, project_key = keys

        # 转换为 Composite Key
        composite_key = f"{project_key}/{session_id}"

        # 1. 追写到会话摘要文件（append-only）
        transcript_path = self._get_transcript_path(session_id, project_key)
        lines = self._read_transcript(transcript_path)
        # SessionStoreEntry 是 TypedDict (dict)，使用 json.dumps 序列化
        lines.extend([json.dumps(entry) for entry in entries])
        self._write_transcript(transcript_path, lines)

        # 2. 更新内存缓存
        self._cache[composite_key] = {
            "mtime": asyncio.get_event_loop().time() * 1000,
            "entries": lines,
        }

        # 3. 更新会话摘要（维护 summary_hint 作为 title）
        summary = self._update_summary(session_id, project_key, entries)
        if summary:
            self._summary_cache[composite_key] = summary

    async def load(
        self,
        key: SessionKey,
    ) -> list[SessionStoreEntry] | None:
        """加载完整会话

        Args:
            key: 会话键（project_key/session_id）

        Returns:
            会话条目列表，如果会话不存在返回 None
        """
        keys = self._require_keys(key)
        if keys is None:
            return None
        session_id, project_key = keys

        # 转换为 Composite Key
        composite_key = f"{project_key}/{session_id}"

        # 1. 尝试从缓存加载
        if composite_key in self._cache:
            return [json.loads(line) for line in self._cache[composite_key]["entries"]]

        # 2. 从文件加载
        transcript_path = self._get_transcript_path(session_id, project_key)
        lines = self._read_transcript(transcript_path)

        if not lines:
            return None

        entries = [json.loads(line) for line in lines]

        # 更新缓存
        self._cache[composite_key] = {
            "mtime": asyncio.get_event_loop().time() * 1000,
            "entries": lines,
        }

        return entries

    # ── 可选方法 ──

    async def delete(self, key: SessionKey) -> None:
        """删除会话（可选实现）"""
        keys = self._require_keys(key)
        if keys is None:
            return
        session_id, project_key = keys

        composite_key = f"{project_key}/{session_id}"

        # 删除会话目录
        session_dir = self.base_dir / project_key / session_id
        if session_dir.exists():
            import shutil

            shutil.rmtree(session_dir)

        # 从缓存中移除
        self._cache.pop(composite_key, None)
        self._summary_cache.pop(composite_key, None)

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        """列出项目会话列表

        Returns:
            会话列表（按 mtime 降序）
        """
        project_dir = self.base_dir / project_key
        if not project_dir.exists():
            return []

        entries: list[SessionStoreListEntry] = []

        for session_id in project_dir.iterdir():
            if not session_id.is_dir():
                continue

            # 读取摘要文件获取 mtime
            summary_path = session_id / ".summary.json"
            if summary_path.exists():
                stat = summary_path.stat()
                mtime = stat.st_mtime * 1000  # 转换为毫秒时间戳

                # TypedDict 直接构造
                entries.append(
                    SessionStoreListEntry(session_id=session_id.name, mtime=int(mtime))
                )

        # 按 mtime 降序排序
        entries.sort(key=lambda e: e["mtime"], reverse=True)
        return entries

    async def list_session_summaries(
        self,
        project_key: str,
    ) -> list[SessionSummaryEntry]:
        """列出会话摘要列表

        Returns:
            会话摘要列表（按 mtime 降序）
        """
        session_list = await self.list_sessions(project_key)

        summaries: list[SessionSummaryEntry] = []
        for entry in session_list:
            composite_key = f"{project_key}/{entry['session_id']}"

            # 检查缓存
            if composite_key in self._summary_cache:
                summaries.append(self._summary_cache[composite_key])
                continue

            # 从文件加载
            summary_path = self._get_summary_path(entry["session_id"], project_key)
            if summary_path.exists():
                with open(summary_path, encoding="utf-8") as f:
                    summary = json.loads(f.read())
                # TypedDict 直接构造（不使用 model_validate）
                self._summary_cache[composite_key] = SessionSummaryEntry(**summary)
                summaries.append(self._summary_cache[composite_key])

        # 按 mtime 降序排序
        summaries.sort(key=lambda s: s["mtime"], reverse=True)
        return summaries

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """列出会话子路径（可选实现）"""
        # TODO: 当前实现不支持子路径，返回空列表
        return []

    # ── 公共接口 ──

    async def get_session_info(self, key: SessionKey) -> dict[str, Any] | None:
        """获取会话信息（面向 ChatCLI 使用）

        Returns:
            {session_id, title, created_at, turn_count, tool_count} 或 None
        """
        entry = await self.load(key)
        if not entry:
            return None

        # 从第一条消息中提取 title
        title = "未命名会话"
        for msg in entry:
            # TypedDict 使用 dict 访问方式
            if msg.get("type") == "summary" and msg.get("summary"):
                title = msg["summary"]
                break

        # 统计 turn 和 tool 调用
        turn_count = sum(1 for msg in entry if msg.get("type") == "user")
        tool_count = sum(1 for msg in entry if msg.get("type") == "tool_use")

        # 从第一条 user 消息提取 created_at
        created_at = int(asyncio.get_event_loop().time() * 1000)
        for msg in entry:
            if msg.get("type") == "user":
                timestamp = msg.get("timestamp")
                if timestamp:
                    try:
                        from datetime import datetime

                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        created_at = int(dt.timestamp() * 1000)
                    except Exception:
                        pass
                break

        return {
            "session_id": key.get("session_id"),
            "title": title,
            "created_at": created_at,
            "turn_count": turn_count,
            "tool_count": tool_count,
        }

    def rename_session(self, key: SessionKey, new_title: str) -> None:
        """重命名会话（面向 CLI 命令使用）"""
        keys = self._require_keys(key)
        if keys is None:
            return
        session_id, project_key = keys

        # 更新摘要文件
        summary_path = self._get_summary_path(session_id, project_key)
        if summary_path.exists():
            with open(summary_path, "r+", encoding="utf-8") as f:
                summary = json.loads(f.read())
                summary["data"]["summary_hint"] = new_title
                f.seek(0)
                f.write(json.dumps(summary))
                f.truncate()

            # 更新内存缓存
            composite_key = f"{project_key}/{session_id}"
            if composite_key in self._summary_cache:
                self._summary_cache[composite_key]["data"]["summary_hint"] = new_title

    async def load_session_messages(
        self,
        key: SessionKey,
        offset: int = 0,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """加载会话消息列表（简化版，面向 ChatCLI）"""
        entry = await self.load(key)
        if not entry:
            return []

        messages = []
        for msg in entry:
            if offset > 0:
                offset -= 1
                continue
            if limit > 0 and len(messages) >= limit:
                break

            messages.append(
                {
                    "type": msg.type,
                    "content": msg.get("content"),
                    "summary": msg.get("summary"),
                    "timestamp": msg.get("timestamp"),
                }
            )

        return messages

    # ── 内部方法 ──

    def _get_session_dir(self, session_id: str, project_key: str) -> Path:
        """获取会话目录完整路径"""
        return self.base_dir / project_key / session_id

    def _get_transcript_path(self, session_id: str, project_key: str) -> Path:
        """获取会话转录文件路径"""
        return self._get_session_dir(session_id, project_key) / ".transcript.jsonl"

    def _get_summary_path(self, session_id: str, project_key: str) -> Path:
        """获取会话摘要文件路径"""
        return self._get_session_dir(session_id, project_key) / ".summary.json"

    @staticmethod
    def _require_keys(key: SessionKey) -> tuple[str, str] | None:
        """校验会话键完整性，返回 (session_id, project_key) 或 None"""
        session_id = key.get("session_id")
        project_key = key.get("project_key")
        if not session_id or not project_key:
            return None
        return session_id, project_key

    def _read_transcript(self, path: Path) -> list[str]:
        """读取转录文件"""
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    return f.read().splitlines()
        except Exception as e:
            logger.warning(f"Failed to read transcript {path}: {e}")
        return []

    def _write_transcript(self, path: Path, lines: list[str]) -> None:
        """写入转录文件"""
        try:
            # 确保目录存在
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.error(f"Failed to write transcript {path}: {e}")

    def _update_summary(
        self,
        session_id: str,
        project_key: str,
        entries: list[SessionStoreEntry],
    ) -> SessionSummaryEntry | None:
        """更新会话摘要"""
        key = SessionKey(project_key=project_key, session_id=session_id)

        # 检查是否需要更新 summary_hint（即 title）
        summary_hint = None
        for entry in entries:
            # SessionStoreEntry 是 TypedDict (dict)
            if entry.get("type") == "summary" and entry.get("summary"):
                summary_hint = entry["summary"]
                break

        # 调用 fold_session_summary
        try:
            prev_summary = None
            composite_key = f"{project_key}/{session_id}"

            # 从缓存或文件获取前一个摘要
            if composite_key in self._summary_cache:
                prev_summary = self._summary_cache[composite_key]
            else:
                # 从文件重新加载，确保数据一致
                summary_path = self._get_summary_path(session_id, project_key)
                if summary_path.exists():
                    with open(summary_path, encoding="utf-8") as f:
                        prev_summary = SessionSummaryEntry(**json.loads(f.read()))

            new_summary = fold_session_summary(prev_summary, key, entries)

            # 更新 summary_hint（如果需要）
            if summary_hint is not None:
                new_summary["data"]["summary_hint"] = summary_hint

            # 记录 mtime
            import time

            new_summary["mtime"] = int(time.time() * 1000)

            # 写入文件
            summary_path = self._get_summary_path(session_id, project_key)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(new_summary))

            return new_summary
        except Exception as e:
            logger.error(f"Failed to update summary: {e}")
            return None


def create_session_store(base_dir: str | Path | None = None) -> SessionStore:
    """创建 FileSessionStore 实例

    Args:
        base_dir: 会话存储目录，默认 ~/.autoloop/sessions

    Returns:
        SessionStore 实例
    """
    return FileSessionStore(base_dir=base_dir)
