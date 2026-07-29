# 指定执行器续作 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户输入 `@<module_id> <提示词>`（模块已交付过）跳过需求解析，进交付轨以 `claude -p --resume <session_id>` 复用该模块执行器会话执行续作。

**Architecture:** 新增 `executor_registry.py`（`.claude/executors.json`，module_id → session_id，merge 成功后从 executor 日志 init 行捕获写入）。repl `@` 分流命中注册表 → 交付队列 `kind="continuation"` → Governor.continuation_flow 合成单模块 spec → 复用 `_run_delivery`（resume 参数透传到 `build_executor_args` 拼 `--resume`）。

**Tech Stack:** Python 3.11+, claude CLI（glm-5.2 执行器）, prompt_toolkit, pytest (asyncio auto)。

**Spec:** `docs/superpowers/specs/2026-07-29-executor-continuation-design.md`

## Global Constraints

- 注册前提=交付成功：只有 merge 成功的模块才写注册表；日志无 session_id 仅警告不阻塞交付。
- 注册表 JSON 损坏按空表处理，不 crash。
- @未命中：主区提示 + 落回普通 auto 流程，不静默吞。
- 版本 bump 只改 `pyproject.toml`，**不动** `src/autoloop_agent/__init__.py` 的 PackageNotFoundError fallback（保持 "0.0.0"）。
- 测试不拉真 claude CLI / SDK：spawn/merge/worktree 全 mock；spike（Task 0）是唯一真跑 CLI 的任务。
- 续作 prompt 必须单行（Windows `-p` 换行被 Node argv 截断）——复用 `build_module_prompt` 天然满足。
- 提交信息中文、conventional commits 前缀。

---

### Task 0: Spike — 跨 cwd resume 可行性实测

**Files:**
- Create: `docs/superpowers/spikes/2026-07-29-cross-cwd-resume.md`（结论记录）

**Interfaces:**
- Produces: spike 结论文档（可行/不可行 + 证据）。**不可行 → 停止执行后续任务，回用户确认降级方案（上下文重注入）。**

- [ ] **Step 1: 建两个临时目录，目录 A 跑首轮**

```bash
mkdir -p /e/front/html/harness-claude-code/.spike/resume-a /e/front/html/harness-claude-code/.spike/resume-b
cd /e/front/html/harness-claude-code/.spike/resume-a
claude -p "记住暗号:蓝鲸42。只回复ok两个字。" --bare --model glm-5.2 --strict-mcp-config --max-turns 5 --output-format stream-json --verbose --permission-mode acceptEdits 2>&1 | head -5
```

从 stream-json 首行 init（`{"type":"system","subtype":"init","session_id":"..."}`）记录 session_id。

- [ ] **Step 2: 目录 B（不同 cwd）resume 追问**

```bash
cd /e/front/html/harness-claude-code/.spike/resume-b
claude -p --resume <session_id> "暗号是什么?只回答暗号本身。" --bare --model glm-5.2 --strict-mcp-config --max-turns 5 --output-format stream-json --verbose --permission-mode acceptEdits 2>&1 | tail -5
```

- [ ] **Step 3: 判定 + 记录**

- 回复含"蓝鲸42" → **可行**；再对照组：删 resume-a 目录重跑 Step 2 仍命中（模拟 worktree 已删场景）→ 记录"删原 cwd 后仍可行/不可行"。
- 回复不含暗号 / 报错 session not found → **不可行**，记录错误原文。
- 结论写 `docs/superpowers/spikes/2026-07-29-cross-cwd-resume.md`（命令、session_id、输出摘录、判定）。

- [ ] **Step 4: 清理 + Commit**

```bash
rm -rf /e/front/html/harness-claude-code/.spike
git add docs/superpowers/spikes/2026-07-29-cross-cwd-resume.md
git commit -m "test: spike 跨 cwd resume 可行性实测结论"
```

**Gate:** spike 结论"可行"才继续 Task 1；"不可行"停止，报用户确认降级（spec §5）。

**Spike 实测结论（2026-07-29，已记录于 spikes/2026-07-29-cross-cwd-resume.md）：**
跨 cwd resume 不可行，但**同路径重建 cwd 后 resume 可行**（session 查找只看 cwd 路径字符串；目录删除后同路径重建不影响）。落地方案：续作用注册表里的**原 req_id** 重建 worktree（路径确定性 `deliver-<req_id>-<mid>`）= 恢复原 cwd = resume 命中。Task 3 的 `continuation_flow` 因此多收一个 `req_id` 参数并透传进 spec（`_req_id` 键，`_spec_to_modules` 优先吃它）。

---

### Task 1: executor_registry.py + extract_session_id

**Files:**
- Create: `src/autoloop_agent/core/executor_registry.py`
- Test: 新建 `tests/test_executor_registry.py`

**Interfaces:**
- Produces（Task 2/3 消费）:
  - `load_registry(project_dir: str) -> dict`
  - `lookup(project_dir: str, module_id: str) -> dict | None`
  - `register_executor(project_dir: str, module_id: str, session_id: str, req_id: str) -> None`
  - `extract_session_id(log_path: str) -> str | None`
  - 记录格式 `{"session_id": str, "req_id": str, "updated_at": iso str}`，文件 `.claude/executors.json`

- [ ] **Step 1: Write the failing tests**

新建 `tests/test_executor_registry.py`：

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_executor_registry.py -v`
Expected: FAIL `ModuleNotFoundError: autoloop_agent.core.executor_registry`

- [ ] **Step 3: Write minimal implementation**

新建 `src/autoloop_agent/core/executor_registry.py`：

```python
"""执行器注册表:module_id -> 最近交付会话(.claude/executors.json)。供 @模块名 续作 resume。

merge 成功后从 executor 日志(stream-json)init 行捕获 session_id 写入,同名模块新交付覆盖旧
(新上下文总是更全)。JSON 损坏按空表处理,不 crash。
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_executor_registry.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/core/executor_registry.py tests/test_executor_registry.py
git commit -m "feat: 执行器注册表 executor_registry - session_id 捕获+持久化"
```

---

### Task 2: build_executor_args --resume 变体

**Files:**
- Modify: `src/autoloop_agent/core/executor.py:75-97`（`build_executor_args`）
- Test: `tests/test_executor_registry.py`（追加；executor.py 无独立测试文件，就近复用）

**Interfaces:**
- Consumes: 无。
- Produces: `build_executor_args(module_prompt: str, max_turns: int, *, resume_session_id: str | None = None) -> list[str]`（Task 3 消费；resume_session_id 非 None 时 argv 尾部追加 `--resume <id>`）。

- [ ] **Step 1: Write the failing tests**

`tests/test_executor_registry.py` 追加：

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_executor_registry.py -v -k resume`
Expected: FAIL `TypeError: unexpected keyword argument 'resume_session_id'`

- [ ] **Step 3: Write minimal implementation**

`executor.py` `build_executor_args` 改：

```python
def build_executor_args(
    module_prompt: str, max_turns: int, *, resume_session_id: str | None = None
) -> list[str]:
    """组装 claude CLI argv(不含 claude 前缀,spawn_executor 前置 _build_claude_prefix)。

    固定:(原 docstring 各条保留)
    resume_session_id 非 None 时尾部追加 --resume <id>(@模块名 续作复用执行器会话)。
    """
    args = [
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
    if resume_session_id:
        args += ["--resume", resume_session_id]
    return args
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_executor_registry.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/core/executor.py tests/test_executor_registry.py
git commit -m "feat: build_executor_args 支持 --resume 续作变体"
```

---

### Task 3: architect 接线（resume 透传 + merge 后注册 + continuation_flow）

**Files:**
- Modify: `src/autoloop_agent/core/architect.py`（`_run_delivery` / `_run_executors` / `_run_module_slot` / 新增 `continuation_flow` + `lookup_executor` + `_register_executor_session`）
- Test: 新建 `tests/test_continuation.py`

**Interfaces:**
- Consumes: Task 1 `executor_registry`（`lookup`/`register_executor`/`extract_session_id`）；Task 2 `build_executor_args(resume_session_id=...)`。
- Produces:
  - `Governor.continuation_flow(module_id: str, prompt: str, session_id: str, req_id: str) -> AsyncIterator[ChatEvent]`（Task 4 消费；req_id=注册表里的原 req_id，决定 worktree 路径=resume 命中前提）
  - `Governor.lookup_executor(module_id: str) -> dict | None`（Task 4 消费；entry 含 `session_id` 与 `req_id`）
  - `Governor._run_delivery(spec: dict, *, resume: dict[str, str] | None = None)`（resume 为 `module_id -> session_id`，默认 None 行为不变）
  - `_spec_to_modules` 优先吃 `spec["_req_id"]`（无则 `_new_req_id()`，行为不变）

- [ ] **Step 1: Write the failing tests**

新建 `tests/test_continuation.py`：

```python
"""指定执行器续作(architect 层) 单元测试
不拉真 claude CLI:_run_delivery monkeypatch 捕获 spec/resume;注册 helper 用 tmp_path 假日志。
"""
from __future__ import annotations

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
    monkeypatch.setattr(
        Governor, "_spec_to_modules",
        lambda self, spec: ("req-1", spec["modules"]),
    )
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_continuation.py -v`
Expected: FAIL `AttributeError: 'Governor' object has no attribute 'continuation_flow'`

- [ ] **Step 3: Write minimal implementation**

3a. architect.py import 区加：

```python
from autoloop_agent.core.executor_registry import (
    extract_session_id,
    lookup as registry_lookup,
    register_executor,
)
```

3b. `Governor` 加公开方法（`deliver_flow` 附近）：

```python
    def lookup_executor(self, module_id: str) -> dict | None:
        """执行器注册表查询(chat 层 @模块名 分流用)。"""
        return registry_lookup(self.project_dir, module_id)

    async def continuation_flow(
        self, module_id: str, prompt: str, session_id: str, req_id: str
    ) -> AsyncIterator[ChatEvent]:
        """续作段公开入口:合成单模块 spec + resume 执行器会话,走完整交付流程(跳过解析)。
        req_id=原交付 req_id(注册表存):worktree 路径 deliver-<req_id>-<mid> 复原 = resume 命中前提。"""
        module = {
            "module_id": module_id,
            "summary": prompt,
            "acceptance": [],
            "subtasks": [
                {"id": "follow-1", "summary": prompt,
                 "intended_files": [], "acceptance": []}
            ],
            "deps": [],
        }
        spec = {
            "task_summary": prompt,
            "risk_level": "low",
            "modules": [module],
            "_req_id": req_id,
        }
        async for event in self._run_delivery(spec, resume={module_id: session_id}):
            yield event

    def _register_executor_session(
        self, logs_dir: Path, module_id: str, req_id: str
    ) -> str | None:
        """merge 成功后登记执行器会话:扫 attempt 2->0 首个含 init 的日志,写注册表。
        无日志/无 init -> None(不写表)。"""
        for attempt in (2, 1, 0):
            sid = extract_session_id(str(logs_dir / f"executor-{module_id}-{attempt}.log"))
            if sid:
                register_executor(self.project_dir, module_id, sid, req_id)
                return sid
        return None
```

3c. `_run_delivery` 签名与透传：

```python
    async def _run_delivery(
        self, spec: dict, *, resume: dict[str, str] | None = None
    ) -> AsyncIterator[ChatEvent]:
        """交付轨：modules 规范化 -> 校验告警 -> _run_executors 分层执行。
        resume: module_id -> session_id(@模块名 续作),None=全新执行。"""
```
尾部 `self._run_executors(modules, req_id, spec)` 改 `self._run_executors(modules, req_id, spec, resume=resume)`。

3d. `_run_executors` 签名 `async def _run_executors(self, modules, req_id, spec, *, resume: dict[str, str] | None = None)`；`_slot_runner` 调用改：

```python
                    ok = await self._run_module_slot(
                        m, req_id, spec, wt, logs_dir, evq,
                        resume_session_id=(resume or {}).get(m["module_id"]),
                    )
```

merge 成功分支（`merged.append(mid)` + `✅` 事件之后）加：

```python
                old = self.lookup_executor(mid)
                try:
                    sid = self._register_executor_session(logs_dir, mid, req_id)
                    if sid and old and old.get("req_id") != req_id:
                        yield _build_message(
                            f"⚠️ @{mid} 执行器换绑:{old['req_id']} -> {req_id}"
                        )
                except Exception as e:
                    yield _build_message(f"⚠️ 模块 {mid} 执行器会话登记失败({str(e)[:60]})")
```

（换绑警告：同名模块二次交付覆盖注册时告知用户续作目标已切换——2026-07-29 用户确认加这层。）

3e. `_run_module_slot` 签名加 `resume_session_id: str | None = None`，`build_executor_args(prompt, max_turns)` 改 `build_executor_args(prompt, max_turns, resume_session_id=resume_session_id)`。

3f. `_spec_to_modules`（architect.py:562）req_id 生成行改：

```python
        req_id = spec.get("_req_id") or self._new_req_id()
```

（`continuation_flow` 传原 req_id → worktree 同路径；其他调用方 spec 无 `_req_id` → 行为不变。）

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_continuation.py tests/test_governor.py -v`
Expected: 全绿（含既有 governor 测试——`_run_delivery`/`_run_executors`/`_run_module_slot` 默认参数保兼容）

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/core/architect.py tests/test_continuation.py
git commit -m "feat: 续作交付路径 - continuation_flow+resume 透传+merge 后登记执行器会话"
```

---

### Task 4: repl @分流 + drawer target

**Files:**
- Modify: `src/autoloop_agent/chat/drawer.py:29-59`（DeliveryItem + add_item）
- Modify: `src/autoloop_agent/chat/repl.py`（`_on_user_input` / `_enqueue_delivery` / `_run_delivery_item`；import re）
- Test: `tests/test_continuation.py`（追加 repl 层测试）

**Interfaces:**
- Consumes: Task 3 `Governor.lookup_executor` / `Governor.continuation_flow`。
- Produces:
  - `DeliveryItem.target: str = ""`；`DrawerPanel.add_item(text: str, kind: str, *, target: str = "") -> DeliveryItem`
  - `ChatCLI._enqueue_delivery(text: str, kind: str, *, target: str = "") -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_continuation.py` 追加：

```python
# ── repl 层 @分流 ──

import asyncio

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.repl import ChatCLI


def make_cli() -> ChatCLI:
    return ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))


def buf_text(buf: ContentBuffer) -> str:
    return "".join(frag[1] for frag in buf.get_formatted_text())


async def _noop_flow(*a, **k):
    return
    yield


async def test_at_registered_enqueues_continuation():
    """@已注册模块:入交付队列 kind=continuation target=模块名,直跑到 done。"""
    cli = make_cli()
    cli.governor = MagicMock()
    cli.governor.lookup_executor.return_value = {"session_id": "s1", "req_id": "r1"}
    cli.governor.continuation_flow = _noop_flow
    await cli._on_user_input("@auth 把错误码改中文")
    await asyncio.sleep(0.1)
    if cli._delivery_runner and not cli._delivery_runner.done():
        cli._delivery_runner.cancel()
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
    if cli._delivery_runner and not cli._delivery_runner.done():
        cli._delivery_runner.cancel()
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
    if cli._delivery_runner and not cli._delivery_runner.done():
        cli._delivery_runner.cancel()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_continuation.py -v -k "at_ or non_at or target"`
Expected: FAIL（`add_item() got an unexpected keyword argument 'target'` / 断言失败）

- [ ] **Step 3: Write minimal implementation**

3a. `drawer.py`：DeliveryItem 加字段 + add_item 加参：

```python
@dataclass
class DeliveryItem:
    """交付队列项:auto=auto 模式需求;adoption=采纳方案;continuation=@模块名续作。"""

    seq: int
    text: str
    kind: str  # "auto" | "adoption" | "continuation"
    state: str = "pending"
    detail: str = ""  # 调度结果/失败原因摘要(可选,追加在行尾)
    target: str = ""  # continuation: 目标模块 module_id
```

```python
    def add_item(self, text: str, kind: str, *, target: str = "") -> DeliveryItem:
        item = DeliveryItem(seq=self._next_seq, text=text, kind=kind, target=target)
        self._next_seq += 1
        self.items.append(item)
        return item
```

3b. `repl.py` import 区加 `import re`，模块级加：

```python
# @模块名 续作:@name + 空白 + 提示词(无提示词不匹配,落普通流程)
_CONTINUATION_RE = re.compile(r"^@(\S+)\s+(.+)$")
```

3c. `_on_user_input`：answer 模式分支之后、斜杠命令判断之前插入：

```python
        # @模块名 续作分流:注册表命中 -> 交付队列(跳过解析,resume 执行器会话)
        m = _CONTINUATION_RE.match(text)
        if m and self.governor.lookup_executor(m.group(1)):
            self._enqueue_delivery(text, "continuation", target=m.group(1))
            return
        if m:
            self.renderer.render_command_result(
                f"执行器 @{m.group(1)} 未注册(未交付过该模块),按普通需求处理"
            )
            # 不 return:落回普通流程
```

3d. `_enqueue_delivery` 加参：

```python
    def _enqueue_delivery(self, text: str, kind: str, *, target: str = "") -> None:
        """交付类输入入队(⏳待解析)并唤醒 runner。"""
        item = self._drawer.add_item(text, kind, target=target)
        self._delivery_queue.append(item)
        self.tui.invalidate()
        if self._delivery_runner is None or self._delivery_runner.done():
            self._delivery_runner = asyncio.create_task(self._delivery_loop())
```

3e. `_run_delivery_item`：`if item.kind == "adoption":` 分支后加 continuation 分支（else auto 之前）：

```python
            elif item.kind == "continuation":
                entry = self.governor.lookup_executor(item.target)
                if entry is None:
                    item.state = "failed"
                    item.detail = "执行器未注册"
                    self._drawer.append_log(
                        f"✗ 需求 #{item.seq} 执行器 @{item.target} 未注册"
                    )
                    self.tui.invalidate()
                    return
                item.state = "executing"
                self.tui.invalidate()
                prompt = _CONTINUATION_RE.match(item.text).group(2)
                await self._drain_delivery_events(
                    self.governor.continuation_flow(
                        item.target, prompt, entry["session_id"], entry["req_id"]
                    )
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_continuation.py tests/test_drawer.py tests/test_delivery_queue.py -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/chat/drawer.py src/autoloop_agent/chat/repl.py tests/test_continuation.py
git commit -m "feat: @模块名 续作分流 - 输入框指定执行器跳过解析直进交付轨"
```

---

### Task 5: 版本 0.7.0 + 全量回归

**Files:**
- Modify: `pyproject.toml:3`（version）

**Interfaces:**
- Consumes: Task 1-4 全部。
- Produces: 无新接口。

- [ ] **Step 1: 版本 bump**

`pyproject.toml`：`version = "0.6.2"` → `version = "0.7.0"`。
**不动** `src/autoloop_agent/__init__.py`（fallback 保持 "0.0.0"）。

- [ ] **Step 2: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿（295 + 新增）。`tests/test_smoke.py::test_sdk_message_types` 为已知 flaky，单独重跑确认即可，不算失败。

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat: 指定执行器续作 + 版本 0.7.0"
```

---

## Self-Review 记录

- Spec 覆盖：§1 注册表→Task 1；§2 注册时机→Task 3（`_register_executor_session` + merge 分支）；§3 @分流→Task 4；§4 续作路径→Task 3/4；§5 spike→Task 0；§6 缓存说明无需代码；错误处理 4 条→Task 1（JSON 容错）/Task 3（登记失败警告）/Task 4（未注册提示、slot 复用）；测试 7 项→Task 0-4 分布。无缺口。
- 占位符：无 TBD；所有代码步骤含完整代码。
- 类型一致：`lookup(project_dir, module_id)` Task 1 定义 = Task 3 `registry_lookup` 别名消费；`Governor.lookup_executor(module_id)` Task 3 定义 = Task 4 repl 消费；`build_executor_args(prompt, max_turns, *, resume_session_id)` Task 2 定义 = Task 3 `_run_module_slot` 消费；`add_item(text, kind, *, target="")` Task 4 定义 = repl `_enqueue_delivery` 调用一致。
- 兼容：`deliver_flow`/`adopt_flow`/`handle_user_input` 调 `_run_delivery` 不传 resume → 默认 None 行为不变；`_run_module_slot` 新参默认 None 不影响既有调用与测试。
