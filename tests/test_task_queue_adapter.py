"""TaskQueueAdapter 单元测试 - 跨项目 import store + seed 转发 + list_by_req 查询。

不依赖真实 task-service 路径：tmp_path 下写最小 store stub（init_db + seed），
TaskQueueAdapter 指向 tmp_path。隔离 sys.path / sys.modules 污染。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from autoloop_agent.core.task_queue_adapter import TaskQueueAdapter


# ── 最小 store stub（模拟跨项目 task-service/store.py 的契约）──────────

_STUB_STORE = '''\
"""Minimal store stub for testing TaskQueueAdapter."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                req_id TEXT,
                domain TEXT,
                module_id TEXT,
                prompt TEXT,
                intended_files TEXT,
                deps TEXT,
                status TEXT DEFAULT 'pending',
                claimed_at TEXT,
                done_at TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def seed(tasks, db_path=None):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        for t in tasks:
            cur = conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (id, req_id, domain, module_id, prompt, intended_files, deps, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    t["id"],
                    t["req_id"],
                    t.get("domain", ""),
                    t.get("module_id"),
                    t.get("prompt", ""),
                    json.dumps(t.get("intended_files") or []),
                    json.dumps(t.get("deps") or []),
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


def is_req_done(req_id, db_path=None):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE req_id=? AND status!='done' LIMIT 1",
            (req_id,),
        ).fetchone()
        return row is None
    finally:
        conn.close()


def reclaim_stale(lease_seconds, db_path=None):
    init_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE tasks SET status='pending', claimed_at=NULL "
            "WHERE status='claimed' AND claimed_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def has_pending(module_id=None, domain=None, db_path=None):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        where = "WHERE status='pending'"
        params = []
        if module_id is not None:
            where += " AND module_id=?"
            params.append(module_id)
        if domain is not None:
            where += " AND domain=?"
            params.append(domain)
        row = conn.execute("SELECT 1 FROM tasks " + where + " LIMIT 1", params).fetchone()
        return row is not None
    finally:
        conn.close()


def reset_slot_claimed(module_id, domain, db_path=None):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE tasks SET status='pending', claimed_at=NULL "
            "WHERE module_id=? AND domain=? AND status='claimed'",
            (module_id, domain),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
'''


@pytest.fixture
def stub_store_dir(tmp_path):
    """写最小 store.py 到 tmp_path，隔离 sys.path / sys.modules 污染。"""
    (tmp_path / "store.py").write_text(_STUB_STORE, encoding="utf-8")
    svc_dir = str(tmp_path)
    yield svc_dir
    # teardown: 清 sys.modules 缓存的 store + 移除本测试注入的 sys.path 项
    sys.modules.pop("store", None)
    resolved = str(Path(svc_dir).resolve())
    for p in (svc_dir, resolved):
        while p in sys.path:
            sys.path.remove(p)


def _make_tasks(req_id: str = "r1") -> list[dict]:
    return [
        {
            "id": f"{req_id}-t1",
            "req_id": req_id,
            "domain": "backend",
            "module_id": "payment",
            "prompt": "do 1",
            "intended_files": ["a.py"],
            "deps": [],
        },
        {
            "id": f"{req_id}-t2",
            "req_id": req_id,
            "domain": "frontend",
            "module_id": "payment",
            "prompt": "do 2",
            "intended_files": None,
            "deps": [f"{req_id}-t1"],
        },
        {
            "id": f"{req_id}-t3",
            "req_id": req_id,
            "domain": "docs",
            "module_id": "payment",
            "prompt": "do 3",
            "intended_files": None,
            "deps": [],
        },
    ]


# ── 测试 ──────────────────────────────────────────────


def test_seed_inserts_tasks(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    inserted = adapter.seed(_make_tasks())
    assert inserted == 3

    rows = adapter.list_by_req("r1")
    assert len(rows) == 3
    assert all(r["status"] == "pending" for r in rows)
    assert [r["id"] for r in rows] == ["r1-t1", "r1-t2", "r1-t3"]
    assert all(r["module_id"] == "payment" for r in rows)


def test_seed_idempotent(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    tasks = _make_tasks()
    adapter.seed(tasks)
    second = adapter.seed(tasks)
    assert second == 0

    # 重灌不产生重复行
    assert len(adapter.list_by_req("r1")) == 3


def test_missing_store_raises(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    with pytest.raises(RuntimeError) as exc:
        TaskQueueAdapter("/nonexist/dir", db_path)
    assert "未找到" in str(exc.value)


def test_seed_forwards_db_path(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    calls: list[str | None] = []

    class FakeStore:
        def seed(self, tasks, db_path=None):
            calls.append(db_path)
            return len(tasks)

    # 替换为 fake store，验证 seed 把 self.task_db_path 透传给 store.seed
    adapter._store = FakeStore()  # type: ignore[assignment]
    adapter.seed(_make_tasks())
    assert calls == [db_path]
    assert calls[0] == adapter.task_db_path


# ── is_req_done 三态 ──────────────────────────────────


def test_is_req_done_empty_true(stub_store_dir, tmp_path):
    """无任何任务（req_id 不存在）-> True。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    assert adapter.is_req_done("r1") is True


def test_is_req_done_pending_false(stub_store_dir, tmp_path):
    """有 pending/claimed 任务 -> False。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    adapter.seed(_make_tasks("r1"))
    assert adapter.is_req_done("r1") is False


def test_is_req_done_all_done_true(stub_store_dir, tmp_path):
    """全 done -> True。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    adapter.seed(_make_tasks("r1"))
    # 直接把所有任务标 done
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE req_id='r1'")
        conn.commit()
    finally:
        conn.close()
    assert adapter.is_req_done("r1") is True


# ── reclaim_stale ─────────────────────────────────────


def test_reclaim_stale_forwards_db_path(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    calls: list[str | None] = []

    class FakeStore:
        def reclaim_stale(self, lease_seconds, db_path=None):
            calls.append(db_path)
            return 0

    # 替换为 fake store，验证 reclaim_stale 把 self.task_db_path 透传给 store.reclaim_stale
    adapter._store = FakeStore()  # type: ignore[assignment]
    adapter.reclaim_stale(60)
    assert calls == [db_path]
    assert calls[0] == adapter.task_db_path


def test_reclaim_stale_returns_count(stub_store_dir, tmp_path):
    """超时 claimed 被回收 -> 返回回收行数；未超时不动。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    adapter.seed(_make_tasks("r1"))

    # 标 2 个 claimed：一个超时（旧时间）、一个未超时（现在）
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='claimed', claimed_at='2020-01-01T00:00:00+00:00' "
            "WHERE id IN ('r1-t1','r1-t2')"
        )
        conn.execute(
            "UPDATE tasks SET status='claimed', claimed_at=? WHERE id='r1-t3'",
            (now_iso,),
        )
        conn.commit()
    finally:
        conn.close()

    # lease=60s：只有 2020 那批超时 -> 回收 2 行
    assert adapter.reclaim_stale(60) == 2

    rows = {r["id"]: r["status"] for r in adapter.list_by_req("r1")}
    assert rows["r1-t1"] == "pending"
    assert rows["r1-t2"] == "pending"
    assert rows["r1-t3"] == "claimed"


# ── reset_slot_claimed ─────────────────────────────────


def test_reset_slot_claimed_forwards_db_path(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    calls: list[str | None] = []

    class FakeStore:
        def reset_slot_claimed(self, module_id, domain, db_path=None):
            calls.append(db_path)
            return 0

    adapter._store = FakeStore()  # type: ignore[assignment]
    adapter.reset_slot_claimed("m1", "d1")
    assert calls == [db_path]
    assert calls[0] == adapter.task_db_path


def test_reset_slot_claimed_returns_count(stub_store_dir, tmp_path):
    """同槽 claimed 被回收 -> 返回行数；另槽 claimed 不受影响。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    adapter.seed(
        [
            {"id": "a1", "req_id": "r1", "domain": "d1", "module_id": "m1", "prompt": "p", "intended_files": None, "deps": []},
            {"id": "a2", "req_id": "r1", "domain": "d1", "module_id": "m1", "prompt": "p", "intended_files": None, "deps": []},
            {"id": "b1", "req_id": "r1", "domain": "d1", "module_id": "m2", "prompt": "p", "intended_files": None, "deps": []},
        ]
    )
    # 标 m1 槽 1 个 claimed + m2 槽 1 个 claimed
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE tasks SET status='claimed', claimed_at='2024-01-01T00:00:00+00:00' WHERE id='a1'")
        conn.execute("UPDATE tasks SET status='claimed', claimed_at='2024-01-01T00:00:00+00:00' WHERE id='b1'")
        conn.commit()
    finally:
        conn.close()

    # 只回收 m1 槽 -> 1 行
    assert adapter.reset_slot_claimed("m1", "d1") == 1
    rows = {r["id"]: r["status"] for r in adapter.list_by_req("r1")}
    assert rows["a1"] == "pending"
    assert rows["a2"] == "pending"
    # m2 槽 claimed 不受影响
    assert rows["b1"] == "claimed"

    # 再 reset 空 claimed -> 0
    assert adapter.reset_slot_claimed("m1", "d1") == 0


# ── has_pending ────────────────────────────────────────


def test_has_pending_forwards_db_path(stub_store_dir, tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)

    calls: list[str | None] = []

    class FakeStore:
        def has_pending(self, module_id=None, domain=None, db_path=None):
            calls.append(db_path)
            return False

    adapter._store = FakeStore()  # type: ignore[assignment]
    adapter.has_pending("m1", "d1")
    assert calls == [db_path]
    assert calls[0] == adapter.task_db_path


def test_has_pending_affinity(stub_store_dir, tmp_path):
    """指定槽有 pending -> True；空槽 -> False。"""
    db_path = str(tmp_path / "db.sqlite")
    adapter = TaskQueueAdapter(stub_store_dir, db_path)
    adapter.seed(
        [
            {"id": "a1", "req_id": "r1", "domain": "d1", "module_id": "m1", "prompt": "p", "intended_files": None, "deps": []},
        ]
    )
    assert adapter.has_pending("m1", "d1") is True
    assert adapter.has_pending("m2", "d1") is False
