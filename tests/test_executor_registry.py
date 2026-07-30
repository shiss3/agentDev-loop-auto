"""执行器注册表 + 日志 session_id 提取 单元测试"""
from __future__ import annotations

import json

from autoloop_agent.core.executor_registry import (
    extract_session_id,
    load_registry,
    lookup,
    register_executor,
)


def _write_log(path, lines: list[dict | str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) if isinstance(line, dict) else line)
            f.write("\n")


# ── extract_session_id ──

def test_extract_session_id_from_init_line(tmp_path):
    log = tmp_path / "executor-auth-0.log"
    _write_log(log, [
        {"type": "system", "subtype": "init", "session_id": "sess-abc", "tools": []},
        {"type": "assistant", "message": {}},
    ])
    assert extract_session_id(str(log)) == "sess-abc"


def test_extract_session_id_no_init_returns_none(tmp_path):
    log = tmp_path / "x.log"
    _write_log(log, [{"type": "assistant", "message": {}}, {"type": "result"}])
    assert extract_session_id(str(log)) is None


def test_extract_session_id_skips_malformed_lines(tmp_path):
    log = tmp_path / "x.log"
    _write_log(log, ["not json at all", "{broken", {"type": "system", "subtype": "init", "session_id": "s9"}])
    assert extract_session_id(str(log)) == "s9"


def test_extract_session_id_missing_file_returns_none(tmp_path):
    assert extract_session_id(str(tmp_path / "nope.log")) is None


# ── registry ──

def test_register_and_lookup_roundtrip(tmp_path):
    register_executor(str(tmp_path), "auth", "sess-1", "req-1")
    entry = lookup(str(tmp_path), "auth")
    assert entry["session_id"] == "sess-1"
    assert entry["req_id"] == "req-1"
    assert "updated_at" in entry
    reg = json.loads((tmp_path / ".claude" / "executors.json").read_text(encoding="utf-8"))
    assert reg["auth"]["session_id"] == "sess-1"


def test_register_same_module_overwrites(tmp_path):
    register_executor(str(tmp_path), "auth", "sess-old", "req-1")
    register_executor(str(tmp_path), "auth", "sess-new", "req-2")
    assert lookup(str(tmp_path), "auth")["session_id"] == "sess-new"


def test_lookup_missing_returns_none(tmp_path):
    assert lookup(str(tmp_path), "ghost") is None


def test_load_corrupted_json_returns_empty(tmp_path):
    p = tmp_path / ".claude" / "executors.json"
    p.parent.mkdir(parents=True)
    p.write_text("{corrupted", encoding="utf-8")
    assert load_registry(str(tmp_path)) == {}
    assert lookup(str(tmp_path), "auth") is None


# ── build_executor_args --resume 变体 ──

def test_build_executor_args_without_resume():
    """默认不变:无 --resume。"""
    from autoloop_agent.core.executor import build_executor_args
    args = build_executor_args("p", 40)
    assert "--resume" not in args
    assert args[args.index("-p") + 1] == "p"
    assert args[args.index("--max-turns") + 1] == "40"


def test_build_executor_args_with_resume():
    """resume_session_id 命中:argv 尾部追加 --resume <id>,其余不动。"""
    from autoloop_agent.core.executor import build_executor_args
    args = build_executor_args("p", 40, resume_session_id="sess-abc")
    assert args[-2:] == ["--resume", "sess-abc"]
    assert "--disallowed-tools" in args  # 原有尾部字段仍在
