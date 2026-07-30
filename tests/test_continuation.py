"""指定执行器续作(architect 层 + repl 层) 单元测试
不拉真 claude CLI:_run_delivery monkeypatch 捕获 spec/resume;注册 helper 用 tmp_path 假日志。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from claude_agent_sdk import SessionStore

from autoloop_agent.core.architect import Governor


def make_governor(tmp_path) -> Governor:
    return Governor(project_dir=str(tmp_path), session_store=MagicMock(spec=SessionStore))


async def _collect(events) -> list:
    return [e async for e in events]


async def test_continuation_flow_synthesizes_single_module_spec(tmp_path, monkeypatch):
    """continuation_flow:合成单模块 spec(follow-1 子任务)+ resume 映射 + _req_id 透传,走 _run_delivery。"""
    seen = {}

    async def fake_run_delivery(self, spec, *, resume=None):
        seen["spec"] = spec
        seen["resume"] = resume
        return
        yield

    monkeypatch.setattr(Governor, "_run_delivery", fake_run_delivery)
    g = make_governor(tmp_path)
    await _collect(g.continuation_flow("auth", "把错误码改中文", "sess-abc", "req-orig"))
    spec, resume = seen["spec"], seen["resume"]
    assert resume == {"auth": "sess-abc"}
    assert spec["_req_id"] == "req-orig"  # 原 req_id:worktree 同路径=resume 命中前提
    assert spec["task_summary"] == "把错误码改中文"
    modules = spec["modules"]
    assert len(modules) == 1
    m = modules[0]
    assert m["module_id"] == "auth"
    assert m["summary"] == "把错误码改中文"
    assert m["deps"] == []
    assert m["subtasks"] == [
        {"id": "follow-1", "summary": "把错误码改中文", "intended_files": [], "acceptance": []}
    ]


def test_spec_to_modules_honors_req_id_override(tmp_path):
    """_spec_to_modules:spec 带 _req_id 用它(续作同路径);不带则新生成(行为不变)。"""
    g = make_governor(tmp_path)
    spec = {"_req_id": "req-orig", "modules": [{"module_id": "auth", "subtasks": []}]}
    req_id, _ = g._spec_to_modules(spec)
    assert req_id == "req-orig"
    req_id2, _ = g._spec_to_modules({"modules": []})
    assert req_id2 and req_id2 != "req-orig"


async def test_run_delivery_default_resume_none_unchanged(tmp_path, monkeypatch):
    """_run_delivery 默认 resume=None:透传给 _run_executors,现有调用方零改动。"""
    seen = {}

    async def fake_run_executors(self, modules, req_id, spec, *, resume=None):
        seen["resume"] = resume
        return
        yield

    monkeypatch.setattr(Governor, "_run_executors", fake_run_executors)
    monkeypatch.setattr("autoloop_agent.core.architect.validate_modules", lambda m: [])
    g = make_governor(tmp_path)
    spec = {"task_summary": "t", "modules": [{"module_id": "a", "subtasks": []}]}
    await _collect(g.deliver_flow(spec))
    assert seen["resume"] is None


def test_lookup_executor_delegates_registry(tmp_path):
    """Governor.lookup_executor 透传注册表(project_dir)。"""
    from autoloop_agent.core.executor_registry import register_executor
    register_executor(str(tmp_path), "auth", "sess-1", "req-1")
    g = make_governor(tmp_path)
    assert g.lookup_executor("auth")["session_id"] == "sess-1"
    assert g.lookup_executor("ghost") is None


def test_register_executor_session_from_logs(tmp_path):
    """_register_executor_session:扫 attempt 2->0 首个含 init 的日志,写注册表返 session_id。"""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "executor-auth-0.log").write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-a0"}) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "executor-auth-1.log").write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-a1"}) + "\n",
        encoding="utf-8",
    )
    g = make_governor(tmp_path)
    sid = g._register_executor_session(logs_dir, "auth", "req-9")
    assert sid == "sess-a1"  # 最高 attempt 优先
    from autoloop_agent.core.executor_registry import lookup
    assert lookup(str(tmp_path), "auth")["req_id"] == "req-9"


def test_register_executor_session_no_logs_returns_none(tmp_path):
    """无任何日志 -> None,不写注册表。"""
    g = make_governor(tmp_path)
    assert g._register_executor_session(tmp_path, "auth", "req-9") is None
    assert g.lookup_executor("auth") is None


def test_rebind_warning_pattern(tmp_path):
    """换绑检测模式:merge 分支注册前 lookup 拿旧 req_id,新 req_id 不同 -> 发换绑警告。"""
    from autoloop_agent.core.executor_registry import register_executor
    register_executor(str(tmp_path), "auth", "sess-a", "reqA")
    g = make_governor(tmp_path)
    old = g.lookup_executor("auth")  # merge 分支在注册前做这步
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "executor-auth-0.log").write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-b"}) + "\n",
        encoding="utf-8",
    )
    sid = g._register_executor_session(logs, "auth", "reqB")
    assert sid == "sess-b"
    assert old["req_id"] == "reqA"  # != "reqB" -> 调用方发 ⚠️ 换绑警告
    assert g.lookup_executor("auth")["session_id"] == "sess-b"


# ── repl 层 @分流 ──

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.repl import ChatCLI


def make_cli() -> ChatCLI:
    return ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))


def buf_text(buf: ContentBuffer) -> str:
    return "".join(frag[1] for frag in buf.get_formatted_text())


async def _noop_flow(*a, **k):
    return
    yield


async def _stop_runner(cli: ChatCLI) -> None:
    if cli._delivery_runner and not cli._delivery_runner.done():
        cli._delivery_runner.cancel()


async def test_at_registered_enqueues_continuation():
    """@已注册模块:入交付队列 kind=continuation target=模块名,直跑到 done。"""
    cli = make_cli()
    cli.governor = MagicMock()
    cli.governor.lookup_executor.return_value = {"session_id": "s1", "req_id": "r1"}
    cli.governor.continuation_flow = _noop_flow
    await cli._on_user_input("@auth 把错误码改中文")
    await asyncio.sleep(0.1)
    await _stop_runner(cli)
    item = cli._drawer.items[-1]
    assert item.kind == "continuation"
    assert item.target == "auth"
    assert item.state == "done"


async def test_at_unregistered_warns_and_falls_back():
    """@未注册模块:主区提示 + 落回普通 auto 流程(kind=auto 入队)。"""
    cli = make_cli()
    cli.governor = MagicMock()
    cli.governor.lookup_executor.return_value = None
    cli.governor.is_adoption_input.return_value = False
    cli.governor.parse_flow = _noop_flow
    await cli._on_user_input("@ghost 做点什么")
    await asyncio.sleep(0.1)
    await _stop_runner(cli)
    assert "未注册" in buf_text(cli.content_buffer)
    item = cli._drawer.items[-1]
    assert item.kind == "auto"


async def test_non_at_input_unaffected():
    """非 @ 输入:不触发分流,走原 auto 流程。"""
    cli = make_cli()
    cli.governor = MagicMock()
    cli.governor.is_adoption_input.return_value = False
    cli.governor.parse_flow = _noop_flow
    await cli._on_user_input("普通需求")
    await asyncio.sleep(0.1)
    await _stop_runner(cli)
    cli.governor.lookup_executor.assert_not_called()
    assert cli._drawer.items[-1].kind == "auto"


def test_delivery_item_target_default_empty():
    """DeliveryItem.target 默认空串;add_item 可带 target。"""
    from autoloop_agent.chat.drawer import DrawerPanel
    drawer = DrawerPanel()
    i1 = drawer.add_item("普通需求", "auto")
    assert i1.target == ""
    i2 = drawer.add_item("@auth 改错误码", "continuation", target="auth")
    assert i2.target == "auth"
