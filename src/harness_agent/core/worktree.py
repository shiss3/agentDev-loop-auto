"""交付 worktree 生命周期:创建/提交/合并/清理。单 worktree 串行(都单策略)。"""
from __future__ import annotations

import subprocess

WORKTREE_DIR = ".claude/worktrees"  # 相对 repo_root


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """跑 git,返回 CompletedProcess(capture_output=True, text=True)。失败不抛(调用方判 returncode)。"""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def create_delivery_worktree(repo_root: str, req_id: str) -> str:
    """git worktree add <repo_root>/.claude/worktrees/deliver-<req_id> -b deliver/<req_id>

    若 worktree 路径已存在先 remove --force。返回 worktree 绝对路径。
    """
    from pathlib import Path

    wt_path = str(Path(repo_root) / WORKTREE_DIR / f"deliver-{req_id}")
    # 已存在先清(重试/残留)
    _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
    branch = f"deliver/{req_id}"
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


def merge_worktree_branch(repo_root: str, req_id: str) -> tuple[bool, str]:
    """cwd=repo_root: git merge --no-ff deliver/<req_id>。

    returncode==0 -> (True, '')。!=0(冲突) -> (False, stdout+stderr)。
    """
    res = _git(["merge", "--no-ff", f"deliver/{req_id}"], cwd=repo_root)
    if res.returncode == 0:
        return (True, "")
    return (False, res.stdout + res.stderr)


def remove_worktree(repo_root: str, req_id: str) -> None:
    """git worktree remove --force <path>。忽略错误(清理尽力)。"""
    from pathlib import Path

    wt_path = str(Path(repo_root) / WORKTREE_DIR / f"deliver-{req_id}")
    _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
