"""执行器注册表:module_id -> 最近交付会话(.claude/executors.json)。供 @模块名 续作 resume。

merge 成功后从 executor 日志(stream-json)init 行捕获 session_id 写入,同名模块新交付覆盖旧
(新上下文总是更全)。req_id 一并存:续作靠它复原 worktree 路径(resume 命中前提)。
JSON 损坏按空表处理,不 crash。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

REGISTRY_REL = ".claude/executors.json"


def _path(project_dir: str) -> Path:
    return Path(project_dir) / REGISTRY_REL


def load_registry(project_dir: str) -> dict:
    """读注册表;文件缺失/JSON 损坏 -> 空表。"""
    try:
        return json.loads(_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def lookup(project_dir: str, module_id: str) -> dict | None:
    """查模块执行器会话条目;未注册 -> None。"""
    return load_registry(project_dir).get(module_id)


def register_executor(project_dir: str, module_id: str, session_id: str, req_id: str) -> None:
    """登记/覆盖模块执行器会话。"""
    reg = load_registry(project_dir)
    reg[module_id] = {
        "session_id": session_id,
        "req_id": req_id,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    p = _path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_session_id(log_path: str) -> str | None:
    """从 executor 日志提取首个 stream-json init 行 session_id;无 init/文件缺失 -> None。"""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "system" and rec.get("subtype") == "init":
                    sid = rec.get("session_id")
                    return sid if isinstance(sid, str) and sid else None
    except OSError:
        pass
    return None
