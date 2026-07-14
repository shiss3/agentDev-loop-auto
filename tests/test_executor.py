"""executor 单元测试 - claude -p --bare 子进程 spawn 支持。

覆盖:
- build_loop_prompt: 含 module_id/domain 且单行(无换行)
- build_executor_args: 含所有必需 flag + --disallowed-tools Bash
- write_mcp_config: 写出合法 JSON 且 env.TASK_DB 正确
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock

from harness_agent.core.executor import (
    EXECUTOR_ALLOWED_TOOLS,
    EXECUTOR_MODEL,
    EXECUTOR_SYSTEM_PROMPT,
    build_dispatch_manifest,
    build_executor_args,
    build_loop_prompt,
    write_mcp_config,
)


# ── build_loop_prompt ────────────────────────────────


def test_loop_prompt_contains_module_id_and_domain():
    p = build_loop_prompt("user_auth", "backend")
    assert "user_auth" in p
    assert "backend" in p


def test_loop_prompt_single_line():
    """Windows -p 换行被 Node argv 截断 -> 必须单行。"""
    p = build_loop_prompt("user_auth", "backend")
    assert "\n" not in p
    assert "\r" not in p


def test_loop_prompt_mentions_sentinels_and_tools():
    p = build_loop_prompt("m", "d")
    assert "无更多任务" in p
    assert "暂无可领,请重试" in p
    assert "claim_next_task" in p
    assert "report_task_done" in p


# ── build_executor_args ──────────────────────────────


def test_build_executor_args_contains_all_flags():
    p = build_loop_prompt("m", "d")
    args = build_executor_args(p, "/tmp/mcp-config.json", 40)
    # 必需 flag 全在
    assert "-p" in args
    assert "--bare" in args
    assert "--append-system-prompt" in args
    assert "--model" in args
    assert "--mcp-config" in args
    assert "--strict-mcp-config" in args
    assert "--max-turns" in args
    assert "--output-format" in args
    assert "--verbose" in args
    assert "--permission-mode" in args
    assert "--allowed-tools" in args
    assert "--disallowed-tools" in args


def test_build_executor_args_disallows_bash():
    p = build_loop_prompt("m", "d")
    args = build_executor_args(p, "/tmp/cfg.json", 40)
    i = args.index("--disallowed-tools")
    assert args[i + 1] == "Bash"


def test_build_executor_args_values():
    p = build_loop_prompt("m", "d")
    args = build_executor_args(p, "/tmp/cfg.json", 40)
    assert args[0] == "-p"
    # -p 后紧跟 loop_prompt
    assert args[args.index("-p") + 1] == p
    # --append-system-prompt 后紧跟常量
    assert args[args.index("--append-system-prompt") + 1] == EXECUTOR_SYSTEM_PROMPT
    # --model 后紧跟常量
    assert args[args.index("--model") + 1] == EXECUTOR_MODEL
    # --mcp-config 后紧跟传入路径
    assert args[args.index("--mcp-config") + 1] == "/tmp/cfg.json"
    # --max-turns 后紧跟字符串化数值
    assert args[args.index("--max-turns") + 1] == "40"
    # --output-format stream-json
    assert args[args.index("--output-format") + 1] == "stream-json"
    # --permission-mode acceptEdits
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    # --allowed-tools 后紧跟常量
    assert args[args.index("--allowed-tools") + 1] == EXECUTOR_ALLOWED_TOOLS


# ── build_dispatch_manifest ─────────────────────────


def test_build_dispatch_manifest_fields():
    """清单含完整 argv + 解读字段,且可 json 序列化(供 spawn 前写盘排查)。"""
    p = build_loop_prompt("m", "d")
    args = build_executor_args(p, "/tmp/cfg.json", 40)
    m = build_dispatch_manifest(
        args,
        req_id="r1",
        module_id="m",
        domain="d",
        attempt=0,
        cwd="/wt",
        log_path="/x.log",
        max_turns=40,
    )
    # argv 忠实备份 + prompt 从 argv 提取(就是实际传的)
    assert m["argv"] == args
    assert m["prompt"] == p
    assert m["model"] == EXECUTOR_MODEL
    assert m["system_prompt"] == EXECUTOR_SYSTEM_PROMPT
    assert m["allowed_tools"] == EXECUTOR_ALLOWED_TOOLS
    assert m["disallowed_tools"] == "Bash"
    assert m["mcp"]["config_path"] == "/tmp/cfg.json"
    assert m["mcp"]["strict"] is True
    assert "--bare" in m["flags"]
    # 禁用能力清单非空(用户核心诉求:看清砍了啥)
    assert m["disabled_capabilities"]
    # 可序列化
    json.dumps(m, ensure_ascii=False)


# ── write_mcp_config ─────────────────────────────────


def test_write_mcp_config_valid_json_and_task_db(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    svc_dir = str(tmp_path / "svc")
    out_path = str(tmp_path / "out" / "mcp-config.json")

    ret = write_mcp_config(db_path, svc_dir, out_path)
    assert ret == out_path

    cfg = json.loads((tmp_path / "out" / "mcp-config.json").read_text(encoding="utf-8"))
    assert "mcpServers" in cfg
    assert "task_queue" in cfg["mcpServers"]
    tq = cfg["mcpServers"]["task_queue"]
    assert tq["command"]  # sys.executable 非空
    assert tq["args"] == [os.path.join(svc_dir, "task_service.py")]
    assert tq["env"]["TASK_DB"] == db_path


def test_write_mcp_config_creates_parent_dir(tmp_path):
    out_path = str(tmp_path / "nested" / "deep" / "mcp-config.json")
    write_mcp_config(str(tmp_path / "db.sqlite"), str(tmp_path), out_path)
    # 父目录已创建,文件可读
    cfg = json.loads((tmp_path / "nested" / "deep" / "mcp-config.json").read_text(encoding="utf-8"))
    assert "task_queue" in cfg["mcpServers"]


# ── spawn_executor 句柄关闭 ─────────────────────────


async def test_spawn_executor_closes_log_handle(monkeypatch, tmp_path):
    """spawn_executor:子进程 spawn 后关闭父侧 log_file 句柄(防长驻 L0 跨 delivery 累积)。"""
    from harness_agent.core import executor as ex

    captured = {}

    async def fake_exec(*args, cwd, stdout, stderr):
        captured["log_file"] = stdout
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ex, "_build_claude_prefix", lambda: ["claude"])
    log_path = str(tmp_path / "exec.log")
    await ex.spawn_executor(["-p", "x"], str(tmp_path), log_path)
    assert captured["log_file"].closed
