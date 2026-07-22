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


# ── 编码:子进程 UTF-8 输出在 gbk locale 下不炸 ──────


def test_git_forces_utf8_encoding(monkeypatch):
    """worktree._git 必须显式 encoding='utf-8' + errors='replace'。

    Windows zh-CN text=True 默认 gbk,git/pytest 吐 UTF-8 中文 ->
    communicate 的 _readerthread UnicodeDecodeError,结果丢失。
    """
    from harness_agent.core import worktree as wt

    captured: dict = {}

    def fake_run(*a, **kw):
        captured.update(kw)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    wt._git(["status"], cwd=".")
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_utf8_child_output_under_gbk_locale():
    """真复现:locale 伪装 gbk,子进程写裸 UTF-8 中文。

    未指定 encoding -> gbk 解码炸在 communicate 的 _readerthread(异常不回传,
    stdout 数据丢失);指定 encoding='utf-8' -> 正常解码。
    """
    import locale
    import sys

    monkey_locale = locale.getpreferredencoding
    locale.getpreferredencoding = lambda *a, **kw: "gbk"  # type: ignore[assignment]
    try:
        # 未指定 encoding -> 复现 bug:reader 线程炸,输出丢失
        r_bad = subprocess.run(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write('中文'.encode('utf-8'))"],
            capture_output=True,
            text=True,
        )
        assert r_bad.stdout != "中文"  # 数据丢失(gbk 解码失败)
        # 指定 encoding/errors -> 修复
        r = subprocess.run(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write('中文'.encode('utf-8'))"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert r.stdout == "中文"
    finally:
        locale.getpreferredencoding = monkey_locale  # type: ignore[assignment]


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
