"""交付 worktree 生命周期:创建/提交/合并/清理。每模块一 worktree(并行隔离)。"""
from __future__ import annotations

import subprocess

WORKTREE_DIR = ".claude/worktrees"  # 相对 repo_root


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """跑 git,返回 CompletedProcess(capture_output=True, text=True)。失败不抛(调用方判 returncode)。

    显式 utf-8:Windows zh-CN text=True 默认 gbk,git 本地化中文输出是 UTF-8,
    gbk 解 -> communicate _readerthread UnicodeDecodeError。errors=replace 兜底非 UTF-8 字节。
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _delivery_ref(repo_root: str, req_id: str, module_id: str) -> tuple[str, str]:
    """(worktree 绝对路径, 分支名)。每模块独立:路径 deliver-<req>-<mod>,分支 deliver/<req>/<mod>。"""
    from pathlib import Path

    wt_path = str(Path(repo_root) / WORKTREE_DIR / f"deliver-{req_id}-{module_id}")
    return wt_path, f"deliver/{req_id}/{module_id}"


def create_delivery_worktree(repo_root: str, req_id: str, module_id: str) -> str:
    """git worktree add <repo_root>/.claude/worktrees/deliver-<req_id>-<module_id> -b deliver/<req_id>/<module_id>

    若 worktree 路径已存在先 remove --force。返回 worktree 绝对路径。
    """
    wt_path, branch = _delivery_ref(repo_root, req_id, module_id)
    # 已存在先清(重试/残留)
    _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
    # 分支可能残留(上次未合),先删;失败忽略(不存在)
    _git(["branch", "-D", branch], cwd=repo_root)
    res = _git(["worktree", "add", "-b", branch, wt_path], cwd=repo_root)
    if res.returncode != 0:
        raise RuntimeError(f"worktree add 失败: {res.stderr.strip()}")
    return wt_path


def commit_worktree(worktree_path: str, msg: str) -> bool:
    """cwd=worktree_path: git add -A && git commit -m <msg>。

    返回 True 若产生 commit,False 若无改动(commit 失败因 nothing to commit)。
    """
    _git(["add", "-A"], cwd=worktree_path)
    res = _git(["commit", "-m", msg], cwd=worktree_path)
    return res.returncode == 0


def merge_worktree_branch(repo_root: str, req_id: str, module_id: str) -> tuple[bool, str]:
    """cwd=repo_root: git merge --no-ff deliver/<req_id>/<module_id>。

    returncode==0 -> (True, '')。!=0(冲突) -> (False, stdout+stderr)。
    """
    _, branch = _delivery_ref(repo_root, req_id, module_id)
    res = _git(["merge", "--no-ff", branch], cwd=repo_root)
    if res.returncode == 0:
        return (True, "")
    return (False, res.stdout + res.stderr)


def remove_worktree(repo_root: str, req_id: str, module_id: str) -> None:
    """git worktree remove --force <path>。忽略错误(清理尽力)。"""
    wt_path, _ = _delivery_ref(repo_root, req_id, module_id)
    _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
