"""交付轨执行器:claude -p --bare 子进程 spawn 支持。"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

EXECUTOR_MODEL = "glm-5.2"
EXECUTOR_SYSTEM_PROMPT = (
    "你是无人值守任务执行器,收到指令立即调用工具,不询问/不复述/不等待确认,"
    "任何先确认指示不适用,唯一终止: claim_next_task 返回无更多任务。"
)
EXECUTOR_ALLOWED_TOOLS = (
    "mcp__task_queue__claim_next_task,mcp__task_queue__report_task_done,Write,Read,Edit"
)


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


def build_loop_prompt(module_id: str, domain: str) -> str:
    """D2 单行循环 prompt。执行器绑定槽(module_id,domain)。

    3 sentinel: 无更多任务->结束 / 暂无可领,请重试->再claim / 任务->执行+report+再claim。
    必须单行(Windows -p 换行被 Node argv 截断)。
    """
    return (
        f'循环执行:调用 mcp__task_queue__claim_next_task(module_id="{module_id}",'
        f'domain="{domain}")领任务;'
        "若返回'无更多任务'则立即结束不做任何事;"
        "若返回'暂无可领,请重试'则再次调用claim_next_task重试;"
        "若返回任务文本则按其提示词执行(用Read读/Edit改/Write建目标文件),"
        "完成后调用mcp__task_queue__report_task_done(任务ID),"
        "再调用claim_next_task继续。重复直到'无更多任务'。"
        "不询问不复述不等待确认。"
    )


def write_mcp_config(task_db_path: str, task_service_dir: str, out_path: str) -> str:
    """写 mcp-config.json,env TASK_DB 指向共享 DB(须与 L0 adapter.task_db_path 一致)。

    格式(对齐 task-service/configure.py):
    {mcpServers:{task_queue:{command:sys.executable, args:[<task_service_dir>/task_service.py],
                              env:{TASK_DB:<task_db_path>}}}}
    返回 out_path。
    """
    cfg = {
        "mcpServers": {
            "task_queue": {
                "command": sys.executable,
                "args": [os.path.join(task_service_dir, "task_service.py")],
                "env": {"TASK_DB": task_db_path},
            }
        }
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def build_executor_args(loop_prompt: str, mcp_config_path: str, max_turns: int) -> list[str]:
    """组装 claude CLI argv(不含 claude 前缀,spawn_executor 前置 _build_claude_prefix)。

    固定:
    -p <loop_prompt> --bare --append-system-prompt <EXECUTOR_SYSTEM_PROMPT> --model <EXECUTOR_MODEL>
    --mcp-config <mcp_config_path> --strict-mcp-config --max-turns <max_turns>
    --output-format stream-json --verbose --permission-mode acceptEdits
    --allowed-tools <EXECUTOR_ALLOWED_TOOLS> --disallowed-tools Bash
    (Bash 硬禁:acceptEdits 下 Bash 非自动接受会卡死无人值守执行器)
    """
    return [
        "-p", loop_prompt,
        "--bare",
        "--append-system-prompt", EXECUTOR_SYSTEM_PROMPT,
        "--model", EXECUTOR_MODEL,
        "--mcp-config", mcp_config_path,
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
    domain: str,
    attempt: int,
    cwd: str,
    log_path: str,
    max_turns: int,
) -> dict:
    """组装交付轨调度清单(spawn 前 dump,回答"给执行器传了什么/禁了什么能力")。

    args: build_executor_args 返回的完整 argv(忠实备份,可原样复现命令)。
    其余字段为人类可读解读 -- 用户要看清传给 claude 的提示词 + 被砍掉的能力。
    prompt/mcp_config_path 从 args 提取(就是实际传的,零歧义)。
    """
    return {
        "req_id": req_id,
        "slot": {"module_id": module_id, "domain": domain},
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
            "config_path": args[args.index("--mcp-config") + 1],
            "strict": True,
            "servers": ["task_queue"],
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
            "--strict-mcp-config: 仅允许 task_queue MCP,禁其他(codegraph 等)",
            "--disallowed-tools Bash: 硬禁 Bash(acceptEdits 下 Bash 非自动接受会卡死无人值守)",
            "allowed_tools 仅 5 个: 无 Task(subagent)/WebFetch/Agent/Glob/Grep",
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
