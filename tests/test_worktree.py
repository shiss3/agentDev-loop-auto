"""worktree 单元测试 - 交付 worktree 生命周期。

用 tmp git repo(git init)真 git,不 mock。
覆盖:
- create -> commit -> merge -> remove 全流程
- merge 冲突场景(主分支改同文件)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness_agent.core.worktree import (
    commit_worktree,
    create_delivery_worktree,
    merge_worktree_branch,
    remove_worktree,
)


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """初始化一个带初始 commit 的 git repo,返回 repo_root。"""
    root = str(tmp_path / "repo")
    Path(root).mkdir()
    _git(["init"], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "tester"], cwd=root)
    (Path(root) / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


# ── 全流程 ────────────────────────────────────────────


def test_create_commit_merge_remove(git_repo):
    root = git_repo
    wt = create_delivery_worktree(root, "r1")
    assert Path(wt).exists()
    assert Path(wt).is_dir()

    # worktree 内新建文件 + commit
    (Path(wt) / "feat.py").write_text("print('feat')\n", encoding="utf-8")
    assert commit_worktree(wt, "feat r1") is True

    # 主分支无改动 -> merge 成功
    ok, err = merge_worktree_branch(root, "r1")
    assert ok is True
    assert err == ""
    # merge 后主分支能看到 feat.py
    assert (Path(root) / "feat.py").exists()

    # 无改动 commit -> False
    assert commit_worktree(wt, "empty") is False

    # remove
    remove_worktree(root, "r1")
    assert not Path(wt).exists()


def test_create_worktree_replaces_existing(git_repo):
    """路径残留时 create 先 remove --force 再 add。"""
    root = git_repo
    wt = create_delivery_worktree(root, "r2")
    (Path(wt) / "a.txt").write_text("a", encoding="utf-8")
    commit_worktree(wt, "first")

    # 再次 create 同 req_id(模拟重试/残留)
    wt2 = create_delivery_worktree(root, "r2")
    assert wt2 == wt
    assert Path(wt2).exists()


# ── merge 冲突 ───────────────────────────────────────


def test_merge_conflict_returns_false_with_output(git_repo):
    root = git_repo
    wt = create_delivery_worktree(root, "r3")

    # 双方改同一文件不同内容
    (Path(wt) / "README.md").write_text("worktree side\n", encoding="utf-8")
    commit_worktree(wt, "wt side")

    (Path(root) / "README.md").write_text("main side\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-m", "main side"], cwd=root)

    ok, err = merge_worktree_branch(root, "r3")
    assert ok is False
    assert err != ""
    assert "conflict" in err.lower() or "merge" in err.lower()

    # 清理冲突状态,便于后续
    _git(["merge", "--abort"], cwd=root)
    remove_worktree(root, "r3")


# ── worktree add 失败不被吞 ──────────────────────────


def test_create_delivery_worktree_add_fail_raises(monkeypatch, git_repo):
    """worktree add returncode!=0 -> raise RuntimeError(不被吞,caller 接到'创建失败')。"""
    from harness_agent.core import worktree as wt

    def fake_git(args, cwd):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        if args[:2] == ["worktree", "add"]:
            r.returncode = 1
            r.stderr = "fatal: invalid path"
        return r

    monkeypatch.setattr(wt, "_git", fake_git)
    with pytest.raises(RuntimeError, match="worktree add 失败"):
        create_delivery_worktree(git_repo, "rfail")
