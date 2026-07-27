"""交付轨执行器:claude -p --bare 子进程 spawn 支持。模块粒度 all-at-once,摘要文件续跑。"""
from __future__ import annotations

import asyncio
import datetime
import re
import shutil
import sys
from pathlib import Path

EXECUTOR_MODEL = "glm-5.2"
EXECUTOR_SYSTEM_PROMPT = (
    "你是无人值守模块执行器,收到指令立即执行,不询问/不复述/不等待确认,"
    "任何先确认指示不适用,唯一终止:派发清单内全部子任务完成且摘要文件已更新。"
)
EXECUTOR_ALLOWED_TOOLS = "Write,Read,Edit,Glob,Grep"

SUMMARY_DIR = ".autoloop"


def _build_claude_prefix() -> list[str]:
    """解析 claude CLI argv 前缀(Windows .cmd 须经 cmd /c)。

    shutil.which 走 PATHEXT 解析返 claude.cmd 全路径;None 则抛(明确报错,不 WinError 2 连试)。
    Windows: CreateProcess 不直接认 .cmd(非 PE),须 cmd /c 包一层(npm 全局 CLI 经典坑)。
    非 Windows: claude 是 PE/sh,create_subprocess_exec 直接认。
    """
    if sys.platform == "win32":
        claude = shutil.which("claude")
        if claude is None:
            raise RuntimeError("claude CLI 未在 PATH 找到(shutil.which 返 None)")
        return ["cmd", "/c", claude]
    return ["claude"]


def summary_rel_path(module_id: str) -> str:
    """模块摘要文件 worktree 内相对路径。"""
    return f"{SUMMARY_DIR}/summary-{module_id}.md"


_SUMMARY_DONE_RE = re.compile(r"^- \[x\]\s*([^\s:]+)", re.MULTILINE)


def read_summary_completed(worktree_path: str, module_id: str) -> set[str]:
    """解析摘要文件 '- [x] <id>' 清单;文件缺失 -> 空集。"""
    p = Path(worktree_path) / summary_rel_path(module_id)
    if not p.exists():
        return set()
    return set(_SUMMARY_DONE_RE.findall(p.read_text(encoding="utf-8")))


def build_module_prompt(module: dict, task_summary: str, summary_path: str) -> str:
    """模块派发 prompt:全量子任务清单 + 摘要文件协议。必须单行(Windows -p 换行被 Node argv 截断)。"""
    mid = module["module_id"]
    items = []
    for i, st in enumerate(module["subtasks"], 1):
        acc = ";".join(st.get("acceptance") or [])
        files = ",".join(st.get("intended_files") or [])
        items.append(f"{i})[{st['id']}]{st['summary']}(验收:{acc})(文件:{files})")
    subtasks_text = " ".join(items)
    mod_acc = ";".join(module.get("acceptance") or [])
    return (
        f"你是模块{mid}的全栈执行器,一次性独立完成全部子任务,前端+后端+数据库一体。"
        f"需求背景:{task_summary}。模块目标:{module['summary']}(验收:{mod_acc})。"
        f"子任务清单:{subtasks_text}。"
        f"摘要协议:开始前先Read文件'{summary_path}',若存在跳过其中已勾选[x]的子任务;"
        f"不存在则先Write创建,含全部子任务清单,每项一行'- [ ] id: 摘要';"
        f"每完成一个子任务立即Edit该文件改'- [x]'并附一行笔记;"
        f"上下文随时可能耗尽,该文件是你唯一进度记忆,必须每步更新;只允许改本模块的摘要文件。"
        "子任务顺序由你按依赖自主决定(如先建表再API再前端),逐个完成直到清单全部[x]后立即结束。"
        "不询问不复述不等待确认。"
    )


def build_executor_args(module_prompt: str, max_turns: int) -> list[str]:
    """组装 claude CLI argv(不含 claude 前缀,spawn_executor 前置 _build_claude_prefix)。

    固定:
    -p <module_prompt> --bare --append-system-prompt <EXECUTOR_SYSTEM_PROMPT> --model <EXECUTOR_MODEL>
    --strict-mcp-config(无 --mcp-config = 零 MCP server)--max-turns <max_turns>
    --output-format stream-json --verbose --permission-mode acceptEdits
    --allowed-tools <EXECUTOR_ALLOWED_TOOLS> --disallowed-tools Bash
    (Bash 硬禁:acceptEdits 下 Bash 非自动接受会卡死无人值守执行器)
    """
    return [
        "-p", module_prompt,
        "--bare",
        "--append-system-prompt", EXECUTOR_SYSTEM_PROMPT,
        "--model", EXECUTOR_MODEL,
        "--strict-mcp-config",
        "--max-turns", str(max_turns),
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowed-tools", EXECUTOR_ALLOWED_TOOLS,
        "--disallowed-tools", "Bash",
    ]


def build_dispatch_manifest(
    args: list[str],
    *,
    req_id: str,
    module_id: str,
    attempt: int,
    cwd: str,
    log_path: str,
    max_turns: int,
) -> dict:
    """组装交付轨调度清单(spawn 前 dump,回答"给执行器传了什么/禁了什么能力")。

    args: build_executor_args 返回的完整 argv(忠实备份,可原样复现命令)。
    其余字段为人类可读解读 -- 用户要看清传给 claude 的提示词 + 被砍掉的能力。
    prompt 从 args 提取(就是实际传的,零歧义)。
    """
    return {
        "req_id": req_id,
        "slot": {"module_id": module_id},
        "attempt": attempt,
        "cwd": cwd,
        "log_path": log_path,
        "max_turns": max_turns,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "argv": args,
        "prompt": args[args.index("-p") + 1],
        "system_prompt": EXECUTOR_SYSTEM_PROMPT,
        "model": EXECUTOR_MODEL,
        "allowed_tools": EXECUTOR_ALLOWED_TOOLS,
        "disallowed_tools": "Bash",
        "mcp": {
            "strict": True,
            "servers": [],
        },
        "permission_mode": "acceptEdits",
        "flags": [
            "--bare",
            "--strict-mcp-config",
            "--output-format stream-json",
            "--verbose",
        ],
        "disabled_capabilities": [
            "--bare: 跳过全局 CLAUDE.md / skills / hooks(无人值守防'先问'压制自驱)",
            "--strict-mcp-config 无 --mcp-config: 零 MCP server(codegraph 等全禁)",
            "--disallowed-tools Bash: 硬禁 Bash(acceptEdits 下 Bash 非自动接受会卡死无人值守)",
            "allowed_tools 仅 5 个: 无 Task(subagent)/WebFetch/Agent",
        ],
    }


async def spawn_executor(args: list[str], cwd: str, log_path: str) -> asyncio.subprocess.Process:
    """spawn claude 子进程。前置 _build_claude_prefix()(Windows cmd /c 包 .cmd)。

    stdout+stderr 重定向到 log_path(per-executor 日志,L0 不解析 stream-json,
    靠退出码+DB 判状态)。
    """
    prefix = _build_claude_prefix()
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            *prefix,
            *args,
            cwd=cwd,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        log_file.close()
    return proc
