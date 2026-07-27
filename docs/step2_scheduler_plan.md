# Step 2 实施计划 - 调度器（Governor 交付轨灌队列）

> ⚠️ **已废弃（0.5.0）**：task_queue MCP 灌队列方案已被「模块粒度 all-at-once 派发 + worktree 摘要文件恢复」取代（subtask DAG/domain/task_queue_adapter 全删）。本文仅作历史存档。

> **范围**：把 `Governor._run_delivery` 从占位实装为「拆任务 -> `store.seed` 灌队列 -> 返回 req_id」，并把 `Governor` 接入 REPL。**不 spawn 执行器**（后续层解耦）。
>
> **三步定位**：①取任务 MCP（`task-service/`，已完成✅）②**调度器（本步）**③记忆系统（后置）。
>
> **基线**：`Governor` 骨架已建（`_parse_requirement` + `_decide_track` + `handle_user_input` + `_build_resident_session` + `_run_delivery` 占位），`test_governor.py` 15 测试过。

---

## 1. 架构定位与边界

### 1.1 调度器职责（L0 = Governor）

```
常驻 TUI 窗口收需求
  │
  ▼
_parse_requirement（stateless query，一次 LLM）
  → spec{task_summary, acceptance_criteria, risk_level, suggest_track, subtasks[]}
  │
  ▼
_decide_track（纯规则）
  ├── 简单（subtasks 空 / suggest_track=interactive）
  │     → 交互轨：常驻执行体 self._resident.send(text) 直接跑（已有）
  └── 复杂（subtasks 非空 / suggest_track=delivery）
        → 交付轨：subtasks → task_queue tasks → store.seed 灌队列 → 返回 req_id
                                                    │
                                                    ▼
                                          （调度器到此结束，不 spawn 执行器）
```

### 1.2 解耦边界（已确认）

- **交付轨 = 拆任务 + 灌队列 + 返回 req_id**。不 spawn claude code cli、不建 worktree、不 merge、不验收。
- 执行器（长驻 claude code + MCP 自取）属**后续层**，本步不碰。
- 验证靠 mock adapter + 查 DB + 可选手动启执行器（`task-service/test_smoke.py` 已证下游可跑）。

### 1.3 本步不做

| 排除项 | 归属 |
|---|---|
| 执行器 spawn / worktree / merge / 验收 / 修复闭环 | 后续层 |
| 记忆系统（GCH / 跨域记忆） | Step 3 |
| task_queue MCP 本身（store/task_service/configure） | Step 1 已完成 |
| 长驻执行器管理（健康检查/重置/预算监控） | 后续层 |
| gated 早停退避重试（claim 区分真空 vs 门控） | 后续层 |
| `/interactive` `/delivery` 斜杠命令（forced_track 透传） | 可选后置，本步默认自动判定即可 |

---

## 2. 现状基线

| 件 | 状态 | 位置 |
|---|---|---|
| task_queue MCP（store/task_service/configure） | ✅ 完成测试 | `E:\front\html\task-service\task-service\`（跨项目，不进本仓库） |
| `BaseAgentSession` 底座 | ✅ | `core/base_session.py` |
| `Governor` 骨架（解析+判定+交互轨+交付轨占位） | ✅ | `core/architect.py` |
| `test_governor.py` | ✅ 15 测试 | `tests/test_governor.py` |
| 交付轨灌队列 | ❌ 占位 | `Governor._run_delivery` |
| `subtasks` schema 缺 domain/deps/intended_files | ❌ | `TASK_SPEC_SCHEMA` |
| task_queue adapter（跨项目 import store） | ❌ | 待建 `core/task_queue_adapter.py` |
| REPL 接入 Governor | ❌（仍用 `ChatSession`） | `chat/repl.py` `ChatCLI` |

---

## 3. 接口契约

### 3.1 `TASK_SPEC_SCHEMA` 扩展（`core/architect.py`）

subtasks 项加 `id` / `domain` / `intended_files` / `deps`，对齐 task_queue：

```python
TASK_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "task_summary": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "suggest_track": {"type": "string", "enum": ["interactive", "delivery"]},
        "expected_scope": {"type": "string"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},            # 短 id，如 "t1"（调度器拼全 id）
                    "domain": {"type": "string"},        # 领域：frontend/backend/database/...
                    "summary": {"type": "string"},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "intended_files": {                  # 预期改动文件（可空）
                        "type": "array", "items": {"type": "string"}
                    },
                    "deps": {                            # 依赖的短 id 列表（引用同批 subtasks 的 id）
                        "type": "array", "items": {"type": "string"}
                    },
                },
                "required": ["id", "domain", "summary", "acceptance"],
            },
        },
    },
    "required": ["task_summary", "acceptance_criteria", "risk_level", "suggest_track"],
}
```

> **deps 短 id 映射**：LLM 输出 `subtasks[].deps` 引用同批 `subtasks[].id`（短 id）。调度器灌队列时把短 id `{req_id}-{short_id}` 映射成全 id，`deps` 同步映射。LLM 不必知道全 id。

### 3.2 `REQUIREMENT_PARSER_PROMPT` 改造

关键改动：交付轨时**按领域拆** + **推断依赖**（当前 prompt 明确禁止按技术领域拆，需反转）。

```text
# 角色：L0 需求解析员

你只做一件事：把用户需求转化为结构化、可验证的标准任务单。你不执行需求，不指导实现细节。

## 输出字段
- task_summary：一句话概括任务核心目标。
- acceptance_criteria：结构化验收要点列表，每项必须可验证（能跑命令/测试/接口验证），锚定原始需求。
- risk_level：低/中/高。
- suggest_track：interactive（单模块局部改动）/ delivery（多模块/多领域完整需求）。
- expected_scope：预估影响范围（仅调度参考）。
- subtasks：仅当 suggest_track=delivery 时，按【技术领域 + 业务功能单元】拆分；每项：
  - id：本批内唯一短 id（"t1","t2",...），供 deps 引用。
  - domain：技术领域（frontend/backend/database/docs/...）。
  - summary：该子任务核心目标。
  - acceptance：该子任务可验证的验收要点。
  - intended_files：预期改动的文件路径列表（不确定可留空）。
  - deps：依赖的同批 subtask id 列表（必须先完成的；无依赖留空数组）。
  单模块任务留空数组。

## 纪律
- 严格锚定原始需求，验收项可追溯到需求原文。
- subtasks 的 prompt/summary 必须自包含（执行器看不到你的上下文）。
- 依赖按真实硬依赖填（"先建表再写 API"），软偏好不填。
- 模糊时 suggest_track 取 interactive（开销小，宁可走交互轨）。
- 不指导实现方案、不指定技术栈。
```

### 3.3 task_queue adapter（新建 `core/task_queue_adapter.py`）

隔离跨项目 import。task-service 不进本仓库，路径可变，故动态 import + 配置。

```python
"""task_queue 灌队列适配层 - 跨项目复用 task-service/store.py。

task-service 不进本仓库（跨项目复用），路径经配置注入，运行时动态 import store。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


class TaskQueueAdapter:
    """灌队列适配器。持有 store 模块引用 + 共享 DB 路径。

    配置：
    - task_service_dir：store.py 所在目录（task-service/task-service/）
    - task_db_path：共享 SQLite 路径（须与执行器 mcp-config.json 的 TASK_DB 一致）
    """

    def __init__(self, task_service_dir: str, task_db_path: str) -> None:
        self.task_db_path = task_db_path
        self._store = self._load_store(task_service_dir)

    @staticmethod
    def _load_store(task_service_dir: str):
        """动态 import store.py（不在本仓库，不能用普通 import）。"""
        dir_path = Path(task_service_dir).resolve()
        store_file = dir_path / "store.py"
        if not store_file.exists():
            raise RuntimeError(f"task-service store.py 未找到: {store_file}")
        if str(dir_path) not in sys.path:
            sys.path.insert(0, str(dir_path))
        import store  # type: ignore[import-not-found]
        return store

    def seed(self, tasks: list[dict]) -> int:
        """灌队列。tasks 项: {id, req_id, domain, prompt, intended_files(list|None), deps([id])}。
        返回实际插入行数（INSERT OR IGNORE，可重灌）。
        """
        return self._store.seed(tasks, db_path=self.task_db_path)

    def list_by_req(self, req_id: str) -> list[dict]:
        """查询某 req_id 下所有任务（验证/调试用，非核心）。"""
        # 直接查 DB，不依赖 store 未暴露的接口
        import sqlite3
        conn = sqlite3.connect(self.task_db_path)
        try:
            rows = conn.execute(
                "SELECT id, domain, status, deps FROM tasks WHERE req_id=? ORDER BY rowid",
                (req_id,),
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "domain": r[1], "status": r[2], "deps": r[3]} for r in rows]
```

> **配置来源**：环境变量 `TASK_SERVICE_DIR` + `TASK_DB`（与 `task_service.py` 读 `TASK_DB` 一致）。`Governor.__init__` 读环境变量构造 adapter；缺省时 `handle_user_input` 交付轨降级提示「未配置 task_queue，走交互轨」。

### 3.4 `Governor._run_delivery` 实装（`core/architect.py`）

```python
async def _run_delivery(self, spec: dict) -> AsyncIterator[ChatEvent]:
    """交付轨：subtasks -> task_queue tasks -> store.seed 灌队列 -> 返回 req_id。

    纯解耦：灌完即返回，不 spawn 执行器。
    """
    subtasks = spec.get("subtasks") or []
    if not subtasks:
        # 判定为 delivery 但无 subtasks -> 降级交互轨
        yield _build_message("⚠️ 交付轨未拆出子任务，降级交互轨执行。")
        async for event in self._resident.send(spec.get("task_summary", "")):
            yield event
        return

    if self._task_queue is None:
        yield _build_message(
            "⚠️ 未配置 task_queue（TASK_SERVICE_DIR/TASK_DB），降级交互轨执行。"
        )
        # 此处 spec.task_summary 不含原 text；forced_delivery 场景需调用方保 text
        async for event in self._resident.send(self._last_text):
            yield event
        return

    # 1. 生成 req_id + 短 id -> 全 id 映射
    req_id = self._new_req_id()                    # uuid4 短 id
    id_map = {s["id"]: f"{req_id}-{s['id']}" for s in subtasks}

    # 2. subtasks -> task_queue tasks
    tasks = []
    for s in subtasks:
        tasks.append({
            "id": id_map[s["id"]],
            "req_id": req_id,
            "domain": s.get("domain", "default"),
            "prompt": self._build_task_prompt(s, spec),   # 自包含：summary+acceptance+全局上下文
            "intended_files": s.get("intended_files") or None,
            "deps": [id_map[d] for d in s.get("deps", [])],
        })

    # 3. 灌队列
    inserted = self._task_queue.seed(tasks)

    # 4. 返回 req_id + 任务清单（仅此 summary 给用户，不进常驻 session 历史）
    lines = [f"🌐 已拆 {len(tasks)} 个任务入队（req_id={req_id}，新增 {inserted}）:"]
    for s, t in zip(subtasks, tasks):
        dep_str = f" deps=[{','.join(s.get('deps', []))}]" if s.get("deps") else ""
        lines.append(f"  • [{s.get('domain', '?')}] {t['id']}{dep_str} - {s['summary'][:40]}")
    lines.append("执行器将通过 task_queue MCP 自取执行（后续层）。")
    yield _build_message("\n".join(lines))

@staticmethod
def _build_task_prompt(subtask: dict, spec: dict) -> str:
    """拼自包含任务 prompt（执行器看不到解析上下文）。"""
    acc = "\n".join(f"- {a}" for a in subtask.get("acceptance", []))
    files = subtask.get("intended_files") or []
    files_line = f"\n预期文件: {', '.join(files)}" if files else ""
    return (
        f"# 任务: {subtask['summary']}\n"
        f"## 需求背景\n{spec.get('task_summary', '')}\n"
        f"## 验收标准\n{acc}{files_line}"
    )
```

> **`_last_text` / `_new_req_id`**：`handle_user_input` 入口存 `self._last_text = text`（降级用）；`_new_req_id()` 用 `uuid.uuid4().hex[:8]`。
>
> **`_task_queue`**：`Governor.__init__` 从环境变量构造 `TaskQueueAdapter | None`。

### 3.5 `Governor.__init__` 扩展

```python
def __init__(
    self,
    project_dir: str,
    model: str | None = None,
    *,
    session_store: SessionStore | None = None,
    task_queue: "TaskQueueAdapter | None" = None,   # 新增，可注入（测试 mock）
) -> None:
    self.project_dir = project_dir
    self.model = model
    self._session_store = session_store if session_store is not None else create_session_store()
    self._resident = self._build_resident_session()
    self._task_queue = task_queue if task_queue is not None else self._build_task_queue()
    self._last_text: str = ""

def _build_task_queue(self) -> "TaskQueueAdapter | None":
    """从环境变量构造 adapter；未配置返回 None（交付轨降级）。"""
    svc_dir = os.environ.get("TASK_SERVICE_DIR")
    db_path = os.environ.get("TASK_DB")
    if not svc_dir or not db_path:
        return None
    try:
        return TaskQueueAdapter(svc_dir, db_path)
    except Exception:
        return None
```

### 3.6 REPL 接入 Governor（`chat/repl.py`）

`ChatCLI` 从 `ChatSession` 切到 `Governor`：

```python
# repl.py 改动点
from autoloop_agent.core.architect import Governor

class ChatCLI:
    def __init__(self, ...):
        ...
        self.governor = Governor(
            project_dir=self.project_dir,
            model=self.model,
            session_store=self.session_store,
        )
        # self.session 仍存在（property 穿透），commands.py 零改动

    @property
    def session(self) -> BaseAgentSession:
        """穿透到 Governor 常驻执行体（commands.py 的 cli.session.* 不变）。"""
        return self.governor.session

    def _create_session(self):  # 删除或改为返回 governor
        ...

    async def run(self):
        ...
        await self.governor.start()   # 替代 self.session.start()
        ...

    async def _stream_events(self, prompt: str):
        """走 Governor.handle_user_input（含双轨调度）。"""
        async for event in self.governor.handle_user_input(prompt):
            if asyncio.current_task().cancelled():
                break
            self.renderer.handle(event)

    async def reset_session(self):
        await self._cancel_message_task()
        await self.governor.rebuild()      # 替代 close+重建 ChatSession
        self.content_buffer.clear()
        self._render_welcome()

    async def switch_project(self, new_dir):
        self.project_dir = str(Path(new_dir).resolve())
        await self.governor.rebuild(project_dir=self.project_dir)

    async def switch_model(self, model):
        self.model = model
        self.tui.model_name = model
        await self.governor.rebuild(model=model)

    def _cancel_current_request(self):
        if self.session.is_processing:    # 经 property 穿透
            self.session.cancel()
        ...
```

> **`session_store` 兼容**：`ChatCLI` 的检查点模式（`enable_undo`）/ 会话恢复（`resume_session_id`/`continue_conversation`）参数当前传给 `ChatSession`。`Governor._build_resident_session` 走 `BaseAgentSession`，**不接** `enable_undo`/`resume`/`continue`。接入时这些参数对 Governor 暂不生效（交互轨不恢复历史会话）。**待决**：是否给 `Governor` 透传 `resume_session_id`（让交互轨支持会话恢复）？倾向本步先不透传，保持 Governor 极简，会话恢复后置。

---

## 4. 测试策略

### 4.1 `tests/test_governor.py` 扩展（新增 ~5 测试）

mock `TaskQueueAdapter`（注入 fake），不依赖真实 task-service/DB：

| # | 测试 | 断言 |
|---|---|---|
| 16 | `test_run_delivery_seeds_tasks` | spec 含 2 subtasks（t1 无依赖、t2 deps=[t1]）-> fake adapter.seed 收到 2 tasks；全 id = `{req_id}-t1`/`{req_id}-t2`；t2.deps=[`{req_id}-t1`]；prompt 含 summary+验收 |
| 17 | `test_run_delivery_no_subtasks_degrades` | delivery 但 subtasks 空 -> 降级交互轨（send 被调） |
| 18 | `test_run_delivery_no_queue_degrades` | `task_queue=None` -> 降级交互轨 + 提示 |
| 19 | `test_run_delivery_returns_req_id` | events 含 `req_id=` 且 8 字符 hex |
| 20 | `test_build_task_prompt_self_contained` | `_build_task_prompt` 输出含 task_summary + 每条 acceptance + intended_files |

> 现有 15 测试中 `test_handle_delivery_placeholder`（断言占位消息「Step 5」）需改为断言新交付轨行为（灌队列 + req_id）。

### 4.2 `tests/test_task_queue_adapter.py`（新建）

用真实 `task-service/` 目录（CI 须能访问 `E:\front\html\task-service\task-service\`，或测试 fixture 拷一份 store.py 到临时目录）：

| 测试 | 断言 |
|---|---|
| `test_seed_inserts_tasks` | seed 3 tasks -> `list_by_req` 返回 3 行 status=pending |
| `test_seed_idempotent` | 同 tasks seed 两次 -> 第二次 inserted=0（INSERT OR IGNORE） |
| `test_deps_mapping` | subtasks deps 短 id -> 全 id 正确映射（在 governor 测试覆盖，adapter 只验 seed 直传） |
| `test_missing_store_raises` | `TaskQueueAdapter("/nonexist", db)` -> RuntimeError |

> **CI 路径问题**：task-service 不进本仓库。测试用 `tmp_path` 拷一份 `store.py`（或手写最小 store stub）避免硬依赖外部路径。倾向 stub（adapter 测的是 import + 转发逻辑，不是 store 本身）。

### 4.3 REPL 接入（手动冒烟，交用户验证）

`autoloop chat` 实跑：
- 简单需求（「改个颜色」）-> 交互轨常驻执行（流式渲染正常）。
- 复杂需求（「重构支付链路」）-> 交付轨：TUI 显示 req_id + 任务清单；查 `TASK_DB` 见 N 行 pending。
- `/clear` `/project` `/model` `/stats` `/context` 命令仍可用。

---

## 5. 任务分解

> 沿用既有工作流：派 general-purpose subagent 执行 + 自审。接口契约已锁定，可并行。

### Task A：schema + prompt + `_run_delivery` + adapter
- 改 `core/architect.py`：扩展 `TASK_SPEC_SCHEMA`（§3.1）、改 `REQUIREMENT_PARSER_PROMPT`（§3.2）、`__init__` 加 `task_queue` 参数 + `_build_task_queue` + `_last_text` + `_new_req_id`、实装 `_run_delivery` + `_build_task_prompt`（§3.4/3.5）、`handle_user_input` 入口存 `_last_text`。
- 新建 `core/task_queue_adapter.py`（§3.3）。
- 自审 + `ruff check` + `python -c "from autoloop_agent.core import Governor"`。

### Task B：测试（依赖 Task A 契约，可并行）
- 扩展 `tests/test_governor.py`（§4.1，改 placeholder 测试 + 加 5 测试）。
- 新建 `tests/test_task_queue_adapter.py`（§4.2，stub store）。
- `PYTHONPATH=src python -m pytest tests/test_governor.py tests/test_task_queue_adapter.py -q` 全过。

### Task C：REPL 接入（依赖 Task A）
- 改 `chat/repl.py` `ChatCLI`（§3.6）：切 `Governor`、`session` property 穿透、`_stream_events` 走 `handle_user_input`、`reset_session`/`switch_*` 走 `governor.rebuild`。
- 验证：`ruff check` + 现有 `tests/` 不引入新失败 + 手动冒烟（交用户）。

### 执行顺序
Task A 先行（锁定契约）；Task B + Task C 并行。三 Task 完成后统一跑全量 `pytest` + 手动冒烟。

---

## 6. 风险与应对

| # | 风险 | 应对 |
|---|---|---|
| 1 | task-service 路径/DB 未配置 -> 交付轨静默失效 | `_build_task_queue` 未配置返回 None；`_run_delivery` 检测 None 降级交互轨 + 明确提示 |
| 2 | LLM 拆 subtasks 时 deps 短 id 引用错（引用不存在的 id） | `id_map` 映射时跳过不存在的 deps 项（防御性 `[id_map[d] for d in deps if d in id_map]`）；不阻断灌队列 |
| 3 | LLM 不按领域拆（prompt 纪律未遵守） | prompt 明确「按技术领域+业务功能单元拆」；`domain` 必填；review 时抽查 |
| 4 | adapter 动态 import store 污染 sys.path | 多次构造幂等（`if str(dir_path) not in sys.path`）；测试用 stub 不碰真实路径 |
| 5 | REPL 切 Governor 后 `enable_undo`/`resume` 失效 | 本步不透传，交互轨不恢复历史会话；会话恢复后置（§3.6 待决） |
| 6 | `commands.py` 的 `cli.session.context_provider` 在 Governor 下是默认 provider | `cmd_context` 显示默认 summary，可接受；后置给 Governor 注入 context_provider |
| 7 | 交付轨 summary 写不写常驻 session 历史 | 不写（解耦：调度器不追踪执行）。`_run_delivery` 只 `yield _build_message` 给用户，不调 `self._resident.send` |
| 8 | 同一需求重发 -> 重复灌队列 | `store.seed` 用 `INSERT OR IGNORE`，但 id 含 uuid req_id 每次不同会重复。**接受**（重发=新需求）；后置可加 req_id 去重 |

---

## 7. 验收清单

- [ ] `TASK_SPEC_SCHEMA` subtasks 项含 `id/domain/intended_files/deps`
- [ ] `REQUIREMENT_PARSER_PROMPT` 改为允许按领域拆 + 推断依赖
- [ ] `core/task_queue_adapter.py` 实现 `TaskQueueAdapter`（动态 import store + seed + list_by_req）
- [ ] `Governor.__init__` 加 `task_queue` 参数 + `_build_task_queue`（环境变量）+ `_last_text`/`_new_req_id`
- [ ] `_run_delivery` 实装：subtasks -> 全 id 映射 -> `seed` 灌队列 -> 返回 req_id + 清单；无 subtasks/无 queue 降级
- [ ] `_build_task_prompt` 产出自包含 prompt（summary + acceptance + intended_files）
- [ ] `handle_user_input` 入口存 `_last_text`
- [ ] `chat/repl.py` `ChatCLI` 切 `Governor`：`session` property 穿透、`_stream_events` 走 `handle_user_input`、`reset_session`/`switch_*` 走 `rebuild`
- [ ] 交付轨不调 `self._resident.send`、不 spawn 进程、不建 worktree
- [ ] `tests/test_governor.py` placeholder 测试改 + 5 新测试全过
- [ ] `tests/test_task_queue_adapter.py` 全过（stub store，不依赖外部路径）
- [ ] 全量 `pytest` 不引入新失败
- [ ] `ruff check` clean
- [ ] 手动冒烟：简单需求走交互轨、复杂需求走交付轨（TUI 显示 req_id + 清单 + DB 见 pending）、斜杠命令可用

---

## 8. 待决（review 时定）

1. **交互轨会话恢复**：`Governor` 是否透传 `resume_session_id`/`continue_conversation`/`enable_undo`？本步倾向不透传（极简），后置。
2. **`/interactive` `/delivery` 斜杠命令**：是否本步加（forced_track 透传）？倾向后置（自动判定已够用）。
3. **adapter 配置载体**：环境变量（本步）vs 配置文件 vs `ChatCLI` 构造参数？环境变量最简，与 `task_service.py` 读 `TASK_DB` 一致。
4. **req_id 去重**：重发同需求会重复灌队列。本步接受，后置加去重。
