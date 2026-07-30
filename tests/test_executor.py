"""executor 单元测试 - 模块粒度 claude -p --bare 子进程 spawn 支持。

覆盖:
- build_module_prompt: 含 module_id/全量子任务/摘要协议,单行(无换行)
- read_summary_completed: '- [x] id' 解析,文件缺失 -> 空集
- build_executor_args: 含所有必需 flag,无 --mcp-config,--disallowed-tools Bash
- build_dispatch_manifest: slot 无 domain,mcp.servers 空
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from autoloop_agent.core.executor import (
    EXECUTOR_ALLOWED_TOOLS,
    EXECUTOR_MODEL,
    EXECUTOR_SYSTEM_PROMPT,
    build_dispatch_manifest,
    build_executor_args,
    build_module_prompt,
    read_summary_completed,
    summary_rel_path,
)


def _module() -> dict:
    return {
        "module_id": "user_auth",
        "summary": "用户认证模块",
        "acceptance": ["能登录", "能登出"],
        "subtasks": [
            {"id": "t1", "summary": "建 users 表", "acceptance": ["表存在"],
             "intended_files": ["db/003_users.sql"]},
            {"id": "t2", "summary": "登录 API", "acceptance": ["POST /login 200"],
             "intended_files": ["api/auth.py"]},
        ],
    }


# ── build_module_prompt ──────────────────────────────


def test_module_prompt_single_line():
    """Windows -p 换行被 Node argv 截断 -> 必须单行。"""
    p = build_module_prompt(_module(), "做登录", ".autoloop/summary-user_auth.md")
    assert "\n" not in p
    assert "\r" not in p


def test_module_prompt_contains_all_subtasks_and_protocol():
    p = build_module_prompt(_module(), "做登录", ".autoloop/summary-user_auth.md")
    assert "user_auth" in p
    assert "做登录" in p
    assert "t1" in p and "建 users 表" in p
    assert "t2" in p and "登录 API" in p
    # 摘要协议要素
    assert ".autoloop/summary-user_auth.md" in p
    assert "[x]" in p
    assert "唯一进度记忆" in p


# ── summary 文件解析 ─────────────────────────────────


def test_summary_rel_path():
    assert summary_rel_path("user_auth") == ".autoloop/summary-user_auth.md"


def test_read_summary_completed_missing_file(tmp_path):
    assert read_summary_completed(str(tmp_path), "m") == set()


def test_read_summary_completed_parses_checked(tmp_path):
    d = tmp_path / ".autoloop"
    d.mkdir()
    (d / "summary-m.md").write_text(
        "# 模块 m\n- [x] t1: 建表 — 笔记\n- [ ] t2: API\n- [x] t3: 前端\n",
        encoding="utf-8",
    )
    assert read_summary_completed(str(tmp_path), "m") == {"t1", "t3"}


# ── build_executor_args ──────────────────────────────


def test_build_executor_args_contains_all_flags():
    args = build_executor_args("p", 40)
    assert "-p" in args
    assert "--bare" in args
    assert "--append-system-prompt" in args
    assert "--model" in args
    assert "--strict-mcp-config" in args
    assert "--mcp-config" not in args  # 零 MCP server
    assert "--max-turns" in args
    assert "--output-format" in args
    assert "--verbose" in args
    assert "--permission-mode" in args
    assert "--allowed-tools" in args
    assert "--disallowed-tools" in args


def test_build_executor_args_disallows_bash():
    args = build_executor_args("p", 40)
    i = args.index("--disallowed-tools")
    assert args[i + 1] == "Bash"


def test_build_executor_args_values():
    args = build_executor_args("p", 40)
    assert args[0] == "-p"
    assert args[args.index("-p") + 1] == "p"
    assert args[args.index("--append-system-prompt") + 1] == EXECUTOR_SYSTEM_PROMPT
    assert args[args.index("--model") + 1] == EXECUTOR_MODEL
    assert args[args.index("--max-turns") + 1] == "40"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--allowed-tools") + 1] == EXECUTOR_ALLOWED_TOOLS
    # 全栈模块需搜索工具
    assert "Glob" in EXECUTOR_ALLOWED_TOOLS
    assert "Grep" in EXECUTOR_ALLOWED_TOOLS


# ── build_dispatch_manifest ─────────────────────────


def test_build_dispatch_manifest_fields():
    """清单含完整 argv + 解读字段,且可 json 序列化(供 spawn 前写盘排查)。"""
    args = build_executor_args("p", 40)
    m = build_dispatch_manifest(
        args,
        req_id="r1",
        module_id="m",
        attempt=0,
        cwd="/wt",
        log_path="/x.log",
        max_turns=40,
    )
    assert m["argv"] == args
    assert m["prompt"] == "p"
    assert m["slot"] == {"module_id": "m"}  # 无 domain
    assert m["model"] == EXECUTOR_MODEL
    assert m["system_prompt"] == EXECUTOR_SYSTEM_PROMPT
    assert m["allowed_tools"] == EXECUTOR_ALLOWED_TOOLS
    assert m["disallowed_tools"] == "Bash"
    assert m["mcp"]["strict"] is True
    assert m["mcp"]["servers"] == []
    assert "--bare" in m["flags"]
    assert m["disabled_capabilities"]
    json.dumps(m, ensure_ascii=False)


# ── spawn_executor 句柄关闭 ─────────────────────────


async def test_spawn_executor_closes_log_handle(monkeypatch, tmp_path):
    """spawn_executor:子进程 spawn 后关闭父侧 log_file 句柄(防长驻 L0 跨 delivery 累积)。"""
    from autoloop_agent.core import executor as ex

    captured = {}

    async def fake_exec(*args, cwd, stdout, stderr):
        captured["log_file"] = stdout
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ex, "_build_claude_prefix", lambda: ["claude"])
    log_path = str(tmp_path / "exec.log")
    await ex.spawn_executor(["-p", "x"], str(tmp_path), log_path)
    assert captured["log_file"].closed
