"""task_queue 灌队列适配层 - 跨项目复用 task-service/store.py。

task-service 不进本仓库（跨项目复用），路径经配置注入，运行时动态 import store。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


class TaskQueueAdapter:
    """灌队列适配器。持有 store 模块引用 + 共享 DB 路径。

    配置：
    - task_service_dir：store.py 所在目录（task-service/task-service/）
    - task_db_path：共享 SQLite 路径（须与执行器 mcp-config.json 的 TASK_DB 一致）
    """

    def __init__(self, task_service_dir: str, task_db_path: str) -> None:
        self.task_db_path = task_db_path
        self._store = self._load_store(task_service_dir)

    @staticmethod
    def _load_store(task_service_dir: str):
        """动态 import store.py（不在本仓库，不能用普通 import）。"""
        dir_path = Path(task_service_dir).resolve()
        store_file = dir_path / "store.py"
        if not store_file.exists():
            raise RuntimeError(f"task-service store.py 未找到: {store_file}")
        if str(dir_path) not in sys.path:
            sys.path.insert(0, str(dir_path))
        import store  # type: ignore[import-not-found]

        return store

    def seed(self, tasks: list[dict]) -> int:
        """灌队列。tasks 项: {id, req_id, domain, module_id, prompt, intended_files(list|None), deps([id])}。
        返回实际插入行数（INSERT OR IGNORE，可重灌）。
        """
        return self._store.seed(tasks, db_path=self.task_db_path)

    def list_by_req(self, req_id: str) -> list[dict]:
        """查询某 req_id 下所有任务（验证/调试用，非核心）。"""
        conn = sqlite3.connect(self.task_db_path)
        try:
            rows = conn.execute(
                "SELECT id, domain, module_id, status, deps FROM tasks WHERE req_id=? ORDER BY rowid",
                (req_id,),
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "domain": r[1], "module_id": r[2], "status": r[3], "deps": r[4]} for r in rows]

    def is_req_done(self, req_id: str) -> bool:
        """需求完成判定：req_id 下无 pending/claimed 任务（全 done）-> True。"""
        return self._store.is_req_done(req_id, db_path=self.task_db_path)
