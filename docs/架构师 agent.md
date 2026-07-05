# Harness Agent — 入口架构师 Agent 设计（L0 Router）

> **版本**: v3.1 | **日期**: 2026-07-04 | **架构代号**: Star-Ray (星射线)
> **适用阶段**: Phase 2（极简 Router 版）
> **范围**: 仅 L0 Router 本身（`core/architect.py`）。
>
> **相关文档**：
> - [`phase_summary_and_phase2_plan.md`](./phase_summary_and_phase2_plan.md) — Phase 2 编排计划
> - [`memory_system_design.md`](./memory_system_design.md) — GCH 设计（Phase 3+ 复用）
> - [`星射线 agent 编排.md`](./星射线%20agent%20编排.md) — 远期愿景
> - [`claude-agent-sdk.md`](./claude-agent-sdk.md) — SDK 能力速查

---

## 0. 一句话定位

L0 是一个**极薄的路由器（Router）**。它用一次 LLM 调用判定用户需求是单领域还是多领域，然后二选一：

- **单领域（Fast Lane）**：L0 自己在当前 session 执行（带写权限），不搞领域拆解、不拉外部进程。
- **多领域（Full）**：L0 拆出各领域提示词，分别拉起多个 claude code CLI 子进程并行处理（每个进程一个独立 git worktree），收口 merge。

L0 永远是 L0——不切换人格、不变成 L1。Fast Lane 下它"兼任"执行体，Full 下它只派发。

---

## 1. Phase 2 范围边界（先不管什么）

> 明确排除，避免过度设计。以下全部后置到 Phase 3+，不在本文档展开。

| 排除项 | 原因 | 何时再做 |
|--------|------|---------|
| ❌ GCH / 跨域记忆 | Phase 2 先跑通 Router 骨架；单领域需求不需要它 | Phase 3 |
| ❌ 自建 L1/L2 层 | Full 模式直接交给 claude code 的 subagent 机制 | 不自建，永久复用 claude code |
| ❌ 脑手解耦完整形态（L0 只读） | Fast Lane 要 L0 自执行，必须带写权限 | Phase 3（Full 模式 L0 可收紧为只读 + 派发） |
| ❌ GlobalDAG / 跨需求依赖合并 | Fast 同步执行、Full 各领域独立 worktree，无跨需求依赖 | 需要时再加 |
| ❌ 长轮询 / `_completed_queue` | Full 用 `asyncio.gather` 同步等子进程完成 | 不需要 |
| ❌ 同域收敛 / 资源声明 / merge 兜底（三级防线） | worktree 物理隔离已足够；冲突协调后置 | Phase 3 |
| ❌ 挂起池 / 温启动 | 先冷启动跑通 | Phase 3 |

---

## 2. 设计决策

| # | 决策 | 理由 |
|---|------|------|
| 1 | **L0 唯一职责 = Router** | 回归本质。判 lane + 执行/派发，没有别的。复杂协调机制等需要时再加 |
| 2 | **路由判定用 LLM，不用正则** | 正则太死板——可能本涉及一个领域却匹配到多领域关键词。模型能力足够直接决策，且一次调用可同时输出 lane + 领域提示词 |
| 3 | **路由判定走独立 stateless query** | 用一次 `output_format` 调用输出 `{lane, reason, tasks[]}`，**不进常驻 session 历史**——避免 text 重复、避免路由 JSON 污染后续执行流。Fast 时 `tasks` 为空，Full 时含各 domain + prompt |
| 4 | **Fast Lane：L0 当前 session 自执行** | 单领域噪音小、省 token、保持对话连续。不拉外部进程 |
| 5 | **Full 模式：claude code CLI 子进程 + worktree** | claude code 自带完善的 subagent 识别 + 独立工作区机制，不重造。L0 只做"判领域 + 分 worktree + 派发 + 收结果 + merge" |
| 6 | **动态权限（硬约束，粒度=业务写）** | L0 session 配全工具集，`can_use_tool` 按 lane 拦截：Full/None 模式禁 `Write/Edit/NotebookEdit`（业务代码），**放行 `Bash`**（git worktree/merge 等收口运维必需）。入口重置 lane + None 按最严兜底，防状态残留 |
| 7 | **L0 常驻单 `ClaudeSDKClient` session** | 与 CLI 会话同生命周期。Fast Lane 的执行历史自然沉淀在 session 中 |
| 8 | **worktree 物理隔离** | Full 模式多个子进程并行写同一项目目录必撞车。每个领域一个 git worktree，做完 merge 回主分支。git 原生，成本极低 |
| 9 | **代码层只记元信息，不做状态机** | session_id 映射、worktree 登记表、成本累计。不维护 TaskState 枚举、不做状态判断 |
| 10 | **路由降级:宁可误 fast,不可误 full** | 路由判定异常(schema 不符/网络/解析失败)默认降级 Fast Lane 执行,不阻断用户。fast 是安全兜底;确为多领域时用户重新发送即可重新判定。不搞交互式选择菜单(避免维护 pending 状态) |
| 11 | **worktree 分支名带唯一标识** | `l0-{domain}-{uuid6}`,防同领域重复执行时分支名/路径冲突(必现 bug)。每次执行独立可追溯 |
| 12 | **L0Router 复用 ChatSession 底座（ADR-13）** | 提取 `BaseAgentSession` 公共基类（SDK 客户端生命周期 + retry + cancel + checkpoint + session_store + can_use_tool），L0Router 通过组合持有 `BaseAgentSession` 实例作 resident session，路由判定用独立 transient session。避免重写 570 行 ChatSession，又避免内嵌完整 REPL/session 逻辑 |
| 13 | **路由 transient session 不持久化（ADR-14）** | 路由判定是一次性结构化调用，判定完即丢弃。不做 `session_store` 持久化，不消耗磁盘，避免路由 JSON 残留 |
| 14 | **移除 `spawn_claude_code` 工具（ADR-15）** | Full 模式直接 `asyncio.create_subprocess_exec("claude", "-p", ...)`，不在 SDK `allowed_tools` 里注册自定义工具。CLI 子进程是操作系统级进程，与 SDK 工具集无关 |
| 15 | **常驻 session 持久化 / transient session 不持久化** | 常驻 session（Fast Lane 执行体）走 `session_store` 持久化，跨 CLI 进程可 `resume`；transient 路由 session 一次性丢弃 |

---

## 3. 路由判定:独立 stateless 调用(不污染常驻 session)

L0 收到用户输入后,先用一次**独立的结构化 LLM 调用**判定 lane 并(若 Full)生成领域提示词。

**关键:这次判定走独立 `query()`(一次性、不进常驻 session 历史)**。原因:
- 避免 text 在常驻 session 里重复出现——Fast Lane 执行时 text 才**首次**进入常驻 session
- 避免结构化路由 JSON 混入常驻 session,干扰后续自然语言执行流
- 路由判定可挂只读工具(Read/Glob/Grep)读项目以准确判断领域归属,与执行工具集解耦

```python
ROUTER_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": ["fast", "full"]},
        "reason": {"type": "string"},
        "tasks": {                          # 仅 full 时非空
            "type": "array",
            "items": {"type": "object", "properties": {
                "domain": {"type": "string"},        # frontend | backend | database | ...
                "prompt": {"type": "string"},        # 含①需求②边界③验收的领域提示词
                "intended_files": {"type": "array", "items": {"type": "string"}},
            }, "required": ["domain", "prompt"]},
        },
    },
    "required": ["lane", "reason", "tasks"],
}

# 路由判定 prompt —— 仅给独立 _route query 用,不进常驻 session
ROUTE_JUDGE_PROMPT = """\
# 角色:星射线入口路由器(判定员)

你只做一件事:判断用户需求是单领域还是多领域,输出结构化结果。你不执行需求。

## 判定规则
- 单领域(fast):只涉及一个技术领域(纯前端改色 / 纯后端加接口 / 纯数据库建表)。
- 多领域(full):跨多个领域(如"重构完整支付链路"涉及前端+后端+数据库)
  → 拆出每个领域的独立提示词。

## 纪律
- 领域数量按需求实质判断,不被关键词数量误导。
  例:"把导航栏颜色改成蓝色" = fast,即使出现"导航/颜色/蓝色"多个词。
- 模糊时 lane 判 fast(fast 开销小,宁可走 fast),并在 reason 标注不确定。
- full 时每个 task 的 prompt 必须自包含(①需求 ②边界 ③验收),子进程看不到你的上下文。
- 可用 Read/Glob/Grep 读项目,以准确判断领域归属。
"""
```

常驻 session 用**另一个**执行型 prompt(`EXECUTOR_PROMPT`,见 §5),不承担路由判定。

---

## 4. 动态权限(硬约束)

L0 session 创建时配**全工具集**(Fast Lane 执行需要写权限)。但 Full 模式下 L0 只应派发、不应写**业务代码**——注意是业务代码,不是所有写操作。

### 4.1 粒度:只禁业务写,放行运维

Full 模式下 L0 仍需通过 `Bash` 执行 `git worktree add/remove`、`git merge`、目录清理等**收口运维操作**。一刀切禁 Bash 会让 Full 流程跑不通。因此只禁业务代码级写入工具:

```python
# Full 模式禁止的业务写工具(直接编辑代码文件)
BUSINESS_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

# Bash / Read / Glob / Grep 始终放行(git 运维 + 派发需要)
# spawn_claude_code 已移除（ADR-15）：Full 模式直接 asyncio.create_subprocess_exec
```

### 4.2 状态残留兜底

`_current_lane` 是实例级变量,异常中断后可能残留,导致下一次路由判定还没完成时就沿用了旧 lane。两道保险:
1. **入口重置**:每次 `handle_user_input` 进来先 `self._current_lane = None`
2. **回调兜底**:`_current_lane` 为 `None` 时按**最严格**(等同 full)处理——禁业务写。路由判定阶段用的是独立 query(无 `can_use_tool`),不经过这个回调;`None` 只在异常残留时出现,按最严处理最安全

```python
class L0Router:
    def __init__(self, project_dir, model=None):
        self._current_lane: str | None = None    # "fast" | "full" | None

        # ── 常驻 session（通过 BaseAgentSession 管理）──
        # BaseAgentSession 封装：SDK 客户端生命周期 + retry + cancel +
        # checkpointing + session_store + can_use_tool 回调
        resident_opts = ClaudeAgentOptions(
            system_prompt=EXECUTOR_PROMPT,        # 执行型 prompt(§5)
            allowed_tools=[                       # 全工具集 —— Fast Lane 要用
                "Read", "Glob", "Grep",
                "Write", "Edit", "NotebookEdit",  # 业务写(fast 放行 / full+None 禁)
                "Bash",                           # 运维(fast/full/None 均放行)
            ],
            can_use_tool=self._lane_guard,        # 硬约束:按 lane 拦业务写
            cwd=project_dir,
            model=model,
            session_store=FileSessionStore(...),   # 持久化（常驻 session）
        )
        self._session = BaseAgentSession(options=resident_opts)

        # ── 路由 transient session（按需创建，用完即弃）──
        self._route_session: BaseAgentSession | None = None
        self.project_dir = project_dir
        self.model = model

    async def _lane_guard(self, input, ctx) -> PermissionResult:
        """按当前 lane 拦截业务写。fast 全放行;full 和 None(异常残留)禁业务写。"""
        if self._current_lane != "fast" and input.tool_name in BUSINESS_WRITE_TOOLS:
            return PermissionResult(
                behavior="deny",
                message="当前模式:业务代码修改由 claude code 子进程执行,L0 不得直接编辑代码文件。",
            )
        return PermissionResult(behavior="allow")
```

> ⚠️ `can_use_tool` 回调签名与 `PermissionResult` 字段以 `claude-agent-sdk` v0.2.93 实际 API 为准(实现时核对)。等价替代:`hooks={"PreToolUse": [...]}` 返回 `permissionDecision: "deny"`。
>
> ⚠️ **Bash 逃逸路径**:Full 模式放行 Bash 意味着 L0 理论上可经 `bash -c "echo > x.cpp"` 绕过 Write 限制。Phase 2 接受这一风险——L0 的 cwd 是主项目目录,worktree 物理隔离保证其操作不污染正在运行的子进程工作区;且 Full 模式下常驻 session 几乎不活跃(见 §6)。若需收紧,Phase 3 可加 Bash 命令前缀白名单。

---

## 5. Fast Lane 执行(单领域)

判定为 `fast` 后,L0 在**常驻 session 直接执行**——text 此时**首次**进入常驻 session,全工具已开(权限回调对 fast 全放行),模型按正常 ReAct 循环读写文件、改完即止。表现为一次干净完整的会话,无重复 text、无路由 JSON 残留。

```python
# 常驻 session 的执行型 prompt
EXECUTOR_PROMPT = """\
# 角色:星射线执行体(L0 Executor)

你直接处理用户的开发需求。你有完整的文件读写与命令执行能力。
读项目 → 理解现状 → 修改 → 自验证 → 完成。遇到模糊处先澄清。
"""

    async def handle_user_input(self, text: str) -> AsyncIterator[Message]:
        # 0. 斜杠命令前置拦截 —— 不经过路由判定（P0: SLASH_PRE_INTERCEPT）
        #     /clear, /context, /undo, /help, /exit 等由常驻 session 或 REPL 直接处理
        #     避免路由 LLM 调用浪费 token 且斜杠命令语义与领域路由无关
        if text.startswith('/'):
            self._current_lane = None
            async for msg in self._session.query(text):
                yield msg
            return

        # 1. 入口重置 lane(防异常残留)
        self._current_lane = None

        # 2. 路由判定 —— 独立 stateless query,不进常驻 session 历史
        try:
            decision = await self._route(text)
        except Exception as e:
            # 路由判定是辅助逻辑,失败不应阻断用户 —— 降级 Fast Lane(宁可误 fast)
            yield _build_message(
                f"⚠️ 路由判定异常({str(e)[:80]}),已降级为单领域模式执行。"
                f"若需求确为多领域,重新发送一次即可重新判定。"
            )
            self._current_lane = "fast"
            async for msg in self._session.query(text):
                yield msg
            return

        self._current_lane = decision["lane"]      # 驱动 §4 硬约束生效

        if decision["lane"] == "fast":
            # 3a. Fast Lane:text 首次进常驻 session,直接执行(全工具已开)
            async for msg in self._session.query(text):
                yield msg
            return

        # 3b. Full 模式:见 §6
        async for msg in self._run_full(decision["tasks"]):
            yield msg

    async def _route(self, text: str) -> dict:
        """独立 stateless 路由判定。transient session，用完即弃（ADR-14）。"""
        opts = ClaudeAgentOptions(
            system_prompt=ROUTE_JUDGE_PROMPT,
            allowed_tools=["Read", "Glob", "Grep"],   # 只读,辅助判断领域
            output_format={"type": "json_schema", "schema": ROUTER_DECISION_SCHEMA},
            cwd=self.project_dir,
            model=self.model,
            max_turns=3,
            # 不做 session_store 持久化 —— transient，一次性的路由调用
        )
        async for msg in query(prompt=text, options=opts):
            if isinstance(msg, ResultMessage) and msg.structured_output:
                return msg.structured_output
        raise RuntimeError("路由判定未返回结构化结果")
```

Fast Lane 不做任何拆解、不拉进程。执行完自然回到对话循环,等下一次输入。常驻 session 的对话历史里只有"用户需求 → 执行过程",没有路由判定的结构化 JSON。

---

## 6. Full 模式执行（多领域）

判定为 `full` 后，L0 拆出的每个 task 对应一个独立 git worktree + 一个 claude code CLI 子进程。子进程内部自行用 claude code 的 subagent 机制干活，L0 不干预。

### 6.1 执行流程

```python
    async def _run_full(self, tasks: list[dict]) -> AsyncIterator[Message]:
        yield _build_message(f"🌐 多领域需求，拆出 {len(tasks)} 个领域，并行处理中...")

        # 1. 每个领域分配独立 worktree
        #    分支名带唯一短 id —— 防同领域重复执行时分支名/路径冲突(必现 bug)
        worktrees: list[tuple[dict, str, str]] = []
        try:
            for t in tasks:
                branch = f"l0-{t['domain']}-{uuid.uuid4().hex[:6]}"
                wt = await create_worktree(self.project_dir, branch)
                self._worktree_registry[t['domain']] = wt  # 登记
                worktrees.append((t, wt, branch))

            # 2. 并发拉起 claude code CLI 子进程（各自 worktree 为 cwd）
            results = await asyncio.gather(*[
                self._spawn_cli(t, wt) for t, wt, _ in worktrees
            ], return_exceptions=True)

            # 3. merge 各 worktree 回主分支（异常分支也要清理 worktree，防残留）
            merge_reports = []
            for (t, wt, branch), res in zip(worktrees, results):
                try:
                    if isinstance(res, Exception):
                        merge_reports.append(f"[{t['domain']}] ❌ 子进程异常: {res}")
                        continue
                    ok = await merge_worktree(self.project_dir, wt, branch)
                    merge_reports.append(
                        f"[{t['domain']}] {'✅ 已合并' if ok else '⚠️ 合并冲突，需人工介入'}"
                    )
                finally:
                    await remove_worktree(self.project_dir, wt, branch)
                    self._worktree_registry.pop(t['domain'], None)  # 清理登记

            # 4. 从 ResultMessage.total_cost_usd 累加
            total_cost = 0.0
            for res in results:
                if isinstance(res, str):
                    # res 是 _spawn_cli 返回的 stdout 文本；需从中解析 cost
                    cost = _extract_cost(res)  # core/cli_utils.py
                    total_cost += cost or 0.0
            self._total_cost += total_cost

            # 5. 收口汇总 —— 仅此 summary 作为 assistant 消息写入常驻 session 历史
            #    P0: FULL_RESULT_PERSISTENCE — 中间日志（子进程 stdout、stream-json 行、
            #    领域进度等）仅展示给用户，不写入 self._session 历史。只有最终汇总报告
            #    作为 assistant 消息持久化，供后续 "/context" 等命令查看。
            #    注意：self._session.send() 不在 Full 模式中被调用 —— session 只看到
            #    这条最终 summary。
            summary = "🌐 多领域处理完成：\n" + "\n".join(merge_reports)
            yield _build_message(summary)

        except Exception as e:
            # P0: WORKTREE_ROLLBACK — worktree 创建过程中任何失败，反向清理已创建的
            for t, wt, branch in reversed(worktrees):
                await remove_worktree(self.project_dir, wt, branch)
                self._worktree_registry.pop(t['domain'], None)
            raise
```

### 6.2 CLI 子进程契约

每个子进程是一个独立的 `claude` headless 调用，在各自 worktree 目录下运行。子进程的 cwd 是 worktree 路径，使用 `--cwd` 设置。

```python
    async def _spawn_cli(self, task: dict, worktree: str) -> str:
        """拉起 claude code CLI 子进程执行单个领域任务。返回 stdout 原始文本。

        ADR-15: 直接使用 asyncio.create_subprocess_exec，不在 SDK allowed_tools
        中注册 spawn_claude_code 工具。CLI 子进程是操作系统级进程，与 SDK 工具集无关。

        子进程的 cwd 由 --cwd 控制，不是继承 L0 的 cwd。

        P0: ASYNCIO_TIMEOUT — 带 900s 超时，超时后 SIGTERM → SIGKILL 升级，
        确保子进程不会变成僵尸进程挂死 L0。
        CLI_TIMEOUT 为类常量（默认 900），可在子类或初始化时覆盖。
        """
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", task["prompt"],
            "--cwd", worktree,
            # worktree 下 Glob/Grep 可选（子进程已隔离，文件集由 prompt 约束）
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
            "--output-format", "stream-json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.CLI_TIMEOUT
            )
        except asyncio.TimeoutError:
            # 超时：先 SIGTERM 给优雅退出机会，再 SIGKILL
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise RuntimeError(
                f"claude code 子进程超时（>{self.CLI_TIMEOUT}s）：{task['domain']}"
            )
        if proc.returncode != 0:
            raise RuntimeError(f"claude code 退出码 {proc.returncode}: {stderr.decode()[:500]}")
        if stderr.strip():
            logger.debug("claude code stderr: %s", stderr.decode()[:1000])
        return stdout.decode()  # 返回原始 stream-json，由 _extract_result_text / _extract_cost 解析
```

> ⚠️ headless flag (`-p` / `--allowedTools` / `--output-format` / `--cwd`) 以 Step0 Spike 实测结果为准（见 §12）。`stream-json` 输出解析函数 `_extract_result_text` / `_extract_cost` 位于 `core/cli_utils.py`。
>
> ⚠️ **`--cwd` 是控制子进程 cwd 的正确方式**，不要用 `chdir` 或 `cd`。Step0 需验证 `--cwd` 在 headless 模式下的行为：是否确实切换工作目录、是否会读取 worktree 下的 `.claude/` 配置。
>
> ✅ `_spawn_cli` 不设置 `cwd` 参数（`create_subprocess_exec` 默认继承 L0 的 cwd），因为子进程实例是刚创建的 subprocess，没有自己的 cwd 概念，`--cwd` 由 claude 内部处理。

```python
# core/cli_utils.py — 从 stream-json 输出解析结果

def _extract_result_text(stdout: str) -> str:
    """从 claude code stream-json 输出提取最终结果文本。
    与 Step 0 Spike 输出严格对齐。
    解析方式：逐行解析 JSON，找到 type="result" 或 type="final" 的消息，
    提取 content[0].text。
    非 JSON 行（如 --verbose 日志）应跳过。
    """
    ...

def _extract_cost(stdout: str) -> float | None:
    """从 stream-json 输出解析 total_cost_usd。
    解析方式：同 _extract_result_text 的流式解析，
    从 type="result"/type="final" 消息中提取 total_cost_usd 字段。
    返回美元值，或 None（未找到）。
    """
    ...

def _extract_usage(stdout: str) -> dict | None:
    """从 stream-json 输出提取 token 用量。
    返回 {"input": ..., "output": ...} 或 None。
    """
    ...
```

### 6.3 worktree 隔离

```python
async def create_worktree(project_dir: str, branch: str) -> str:
    """git worktree add，返回新工作区路径。branch 由调用方保证唯一(含 uuid 短 id)。"""
    wt_path = os.path.join(project_dir, ".harness", "worktrees", branch)
    await _git(project_dir, f"worktree add -b {branch} {wt_path}")
    return wt_path

async def merge_worktree(project_dir: str, wt_path: str, branch: str) -> bool:
    """merge 回主分支。返回 True=干净合并，False=冲突。"""
    rc = await _git(project_dir, f"merge --no-ff {branch}", check=False)
    return rc == 0     # 非零退出码 = 冲突（Phase 2 不自动解决，标记需人工介入）

async def remove_worktree(project_dir: str, wt_path: str, branch: str):
    """清理 worktree + 删除临时分支。无论成功/异常都调用，防残留。"""
    await _git(project_dir, f"worktree remove --force {wt_path}", check=False)
    await _git(project_dir, f"branch -D {branch}", check=False)   # 临时分支不再保留
```

Phase 2 不做冲突自动解决——merge 冲突时标记"需人工介入"即可（Phase 3 再加冲突解决子进程兜底）。

---

## 7. 极简状态模型

L0 不维护任务状态机。代码层只记元信息：

```python
# L0Router 实例级属性
_current_lane: str | None = None          # 当前 lane（驱动硬约束。入口重置为 None）
_session: BaseAgentSession                # 常驻执行 session（Fast Lane 积累对话历史）
_route_session: BaseAgentSession | None   # transient 路由 session（按需创建，用完即弃）
_worktree_registry: dict[str, str] = {}   # domain → worktree 路径（Full 运行期，创建时登记、清理时移除）
_total_cost: float = 0.0                  # 累计美元成本（每次 Fast/Full 完成后累加）
CLI_TIMEOUT: int = 900                    # _spawn_cli 子进程超时秒数（P0: ASYNCIO_TIMEOUT）
```

| | L0 常驻 session（BaseAgentSession） | 路由 transient session | claude code 子进程（Full） |
|------|-----------|-----------|-----------|
| **自己的执行动作** | Fast Lane 执行 | 路由判定（一次性 LLM 调用） | 领域内全部工作（claude code 自管 subagent） |
| **来自上层的输入** | 用户输入 | 用户原始 text | L0 下发的领域 prompt |
| **返回给上层** | — | 结构化判定结果 `{lane, reason, tasks[]}` | stream-json 最终结果 |
| **持久化** | `session_store` 持久化（跨 CLI 进程可 resume） | **不持久化**（ADR-14） | 各自独立 session（claude code 自管） |
| **状态源** | 自身 SDK session | 无状态 | 各自独立 session（claude code 自管） |

> ⚠️ `_worktree_registry` 仅在 Full 模式运行期非空。每次 `create_worktree` 时登记、`remove_worktree` 时移除。异常中断后不会残留，因为清理是 finally 块中的逻辑。
>
> `_total_cost` 在每次 Fast Lane 执行或 Full 模式完成后，从 `ResultMessage.total_cost_usd` 累加。Phase 2 仅做记录，不触发预算控制（后置 Phase 3+）。

跨进程通信走 **CLI stdout**（子进程 → L0），不是记忆注入。

---

## 8. L0 完整工作流

```
CLI 会话开始
  │
  ▼
L0Router 初始化 → 常驻 BaseAgentSession（全工具集 + can_use_tool 硬约束 + session_store 持久化）
  │
  ▼ 用户输入到达
  │
  _route(text)  ← 独立 query()调用（一次性、不持久化、无 can_use_tool）
  │                 output_format 判定
  │
  ├── lane == "fast"（单领域）
  │     │
  │     ├→ _current_lane = "fast"
  │     ├→ self._session.query(text)   ← 常驻 session 首次拿到 text
  │     └→ 流式返回，改完即止
  │
  └── lane == "full"（多领域）
        │
        ├→ _current_lane = "full"   ← 硬约束生效：L0 调写工具会被拒
        ├→ 每个 task → git worktree add
        ├→ asyncio.gather(claude -p 子进程 × N)   ← 各 worktree 为 cwd
        │     └─ claude code 内部自管 subagent，L0 不干预
        ├→ 各 worktree merge 回主分支
        └→ 收口汇总给用户
```

> ⚠ 路由判定(`_route`)异常时降级 Fast Lane(见 §5),不阻断用户;worktree 分支名带 uuid 短 id,防同领域重复执行冲突。

**示例：**
- "把导航栏颜色改成蓝色" → `fast` → L0 自己改 CSS，完事。
- "重构完整支付链路" → `full` → 拆出 frontend/backend/database 三份 prompt → 三个 worktree + 三个 claude 子进程并行 → 各自 merge → 汇总。

---

## 9. 后置清单（Phase 3+）

以下能力 Phase 2 不做，待主干稳定后再加：

| 能力 | 触发条件 | 落点 |
|------|---------|------|
| GCH 跨域记忆 | 出现真实的跨领域契约协调需求 | 复用 `memory_system_design.md` |
| Fast Lane 执行预算控制 | Fast Lane 子任务失控超时 | `task_budget` + `max_budget_usd` |
| Full 模式 L0 收紧为只读 | 脑手解耦完整形态 | 调整 `allowed_tools` + 硬约束常驻 |
| merge 冲突自动解决 | worktree merge 频繁冲突 | 冲突解决子进程兜底 |
| 跨需求依赖 DAG | 出现"先建表再写 API"类硬依赖 | `GlobalDAG`（v2.5 设计可复用） |
| 挂起池 / 温启动 | 需要跨会话记忆续接 | SDK `session_store` + `resume` |
| 上下文压缩兜底 | Fast Lane 长任务撑爆上下文 | `get_context_usage` + `PreCompact` |
| 危险工具护栏 | 需按工具类型分级准入 | 扩展 `can_use_tool` |

---

## 10. 风险与应对

| 风险 | 应对 |
|------|------|
| 路由判定误判（fast 误判 full 或反之） | 判定带 `reason` 字段可审计；用户可即时纠正；宁可 fast 也别漏（fast 开销小） |
| Full 模式 LLM 越界写业务代码 | `can_use_tool` 硬约束 deny `Write/Edit/NotebookEdit`，不靠 prompt 自觉 |
| Full 模式 Bash 逃逸（经 bash 写文件绕过 Write） | Phase 2 接受：worktree 物理隔离兜底，L0 cwd 在主目录不污染子进程工作区；Phase 3 加 Bash 命令白名单收紧 |
| `_current_lane` 异常残留导致权限错乱 | 入口 `handle_user_input` 重置为 None；回调对 None 按最严（禁业务写）兜底 |
| Fast Lane 长任务撑爆 L0 session 上下文 | Phase 2 先观察；触发后置 §9 的压缩兜底预算控制 |
| 多 worktree merge 冲突 | Phase 2 标记"需人工介入"；Phase 3 加冲突解决子进程 |
| claude code CLI flag 随版本变动 | headless 调用封装在 `_spawn_cli`，单一适配点 |
| 子进程异常退出 | `gather(return_exceptions=True)` 捕获，汇总报告标注 ❌；异常分支也清理 worktree 防残留；`_worktree_registry` 在清理时统一移除确保无残留 |
| 路由判定异常中断整个流程 | `try/except` 捕获,降级 Fast Lane 执行;提示用户可重新发送重判(决策 10) |
| 同领域重复执行 worktree 命名冲突 | 分支名带 uuid 短 id(`l0-{domain}-{uuid6}`),每次执行独立(决策 11) |
| `create_subprocess_exec` 无 `cwd` 参数，`--cwd` 依赖 claude CLI 内部处理 | Step0 验证 `--cwd` 在 headless 模式下的行为；若 `--cwd` 不可用，改用 `subprocess.Popen` 的 `cwd` 参数，或 `os.chdir` 前 pop 后 restore |
| `_spawn_cli` 返回原始 stdout 而非提取结果，调用方需自行解析 | 设计如此——`_spawn_cli` 只负责子进程生命周期，结果解析由 `_run_full` 调用 `_extract_result_text`/`_extract_cost` 完成，单一职责 |
| **P0: ASYNCIO_TIMEOUT — `_spawn_cli` 无超时，子进程可能僵尸挂死 L0** | 增加 `asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)` 包装；超时后 SIGTERM→SIGKILL 升级；触发 worktree cleanup；`CLI_TIMEOUT` 默认可配（900s） |
| **P0: WORKTREE_ROLLBACK — 循环创建 worktree 时部分失败残留垃圾文件** | worktree 创建循环包在 `try/except` 中，发生异常时按逆序遍历已创建的 `worktrees` 列表并清理，再 re-raise |
| **P0: SLASH_PRE_INTERCEPT — 斜杠命令未前置拦截，无效消耗路由 token** | `handle_user_input` 入口增加 `text.startswith('/')` 检查，跳过 `_route()` 直接委托常驻 session 处理 |
| **P0: FULL_RESULT_PERSISTENCE — Full 模式结果写入 session 历史规则未定义** | 仅最终 summary（`yield _build_message` 行）作为 assistant 消息写入常驻 session 历史；中间日志显示但不持久化；Full 模式不调 `self._session.send()` |

---

## 12. Claude Code CLI Headless Contract（待 Step0 Spike 后填充）

> 本节记录 `claude` CLI 作为 headless 子进程的**实际契约**，由 Step0 Spike 验证后填写。
> 实现时以本节为准，不依赖文档推测或 --help 输出。

| 验证项 | 预期 | 实测结果 | 备注 |
|--------|------|---------|------|
| `-p` 接受内联 prompt | 单轮 headless 执行，不进入交互模式 | ⏳ Step0 | |
| `--cwd <path>` 设置工作目录 | 子进程 cwd 切换为 worktree 路径 | ⏳ Step0 | |
| `--allowedTools` 限制工具集 | 子进程只能使用指定工具 | ⏳ Step0 | |
| `--output-format stream-json` | 输出为 JSON Lines，含 `type`/`content` 字段 | ⏳ Step0 | |
| `--verbose` 对 stream-json 的影响 | 是否混入非 JSON 行（需 stderr 分离） | ⏳ Step0 | |
| stderr 是否混入 stdout | 若混入需从 stream-json 中过滤非 JSON 行 | ⏳ Step0 | |
| 退出码语义 | 0=成功，非0=异常 | ⏳ Step0 | |
| 超时/中断行为 | SIGTERM → 退出码？输出是否完整？ | ⏳ Step0 | |
| stream-json 中最终结果字段 | 取 `result` 消息的 `content[0].text`？ | ⏳ Step0 | |
| 子进程内 subagent 是否自动启用 | `-p` 模式下 subagent 机制是否生效 | ⏳ Step0 | |

> **实现注意事项**：
> - `_spawn_cli()` 中对 `stdout` 和 `stderr` 分别 pipe，防止 stream-json 被日志污染。
> - `_extract_result_text()` 从 stream-json 解析最终结果，位于 `core/cli_utils.py`。
> - 若 `--allowedTools` 在 headless 模式下不支持，改用 `--dangerously-skip-permissions` + prompt 约束。

---

## 12. 验收标准

| # | 项 | 通过条件 |
|---|------|--------|
| 1 | **极薄 Router** | `core/architect.py` 核心逻辑 ≤ ~250 行；无 GlobalDAG / 长轮询 / Analyst 分裂 |
| 2 | **LLM 路由判定** | 一次 `output_format` 调用输出 `{lane, reason, tasks[]}`，不依赖正则 |
| 3 | **Fast Lane 自执行** | 单领域需求由 L0 当前 session 直接完成（带写权限） |
| 4 | **Full 模式并发** | 多领域需求拆出 N 份 prompt，并发拉起 N 个 claude code 子进程 |
| 5 | **worktree 隔离** | 每个 Full 子进程在独立 git worktree 执行，互不干扰 |
| 6 | **merge 收口** | 各 worktree 变更 merge 回主分支，冲突标记需人工介入 |
| 7 | **动态权限硬约束** | Full 模式下 L0 调用 `Write/Edit/NotebookEdit` 被 `can_use_tool` 拒绝，`Bash` 放行（运维）；Fast Lane 下全部放行；`_current_lane=None` 时按 Full 最严处理 |
| 8 | **CLI 子进程契约** | `claude -p` headless 调用成功执行并解析输出 |
| 9 | **无后置机制残留** | 代码中无 GCH 调用、无 `TaskState` 枚举、无 `_inject_completed_results` |
| 10 | **BaseAgentSession 组合** | L0Router 通过 `self._session`（BaseAgentSession 实例）而非直接持有 `ClaudeSDKClient`；路由判定用独立 transient session |
| 11 | **路由 session 不持久化** | transient route session 无 `session_store`，判定完成后丢弃，无磁盘残留 |
| 12 | **remove spawn_claude_code** | `allowed_tools` 中不含 `spawn_claude_code`，Full 模式使用 `asyncio.create_subprocess_exec` |
| 13 | **子进程超时兜底（P0: ASYNCIO_TIMEOUT）** | `_spawn_cli` 调用 `asyncio.wait_for` 包裹 `proc.communicate()`，超时后 SIGTERM→SIGKILL 升级，同时触发 worktree 清理；`CLI_TIMEOUT` 可配置（默认 900s） |
| 14 | **worktree 创建回滚（P0: WORKTREE_ROLLBACK）** | `_run_full` 中 worktree 创建循环包裹在 try/except 中，失败时逆序清理已创建的 worktrees 再 re-raise |
| 15 | **斜杠命令前置拦截（P0: SLASH_PRE_INTERCEPT）** | `handle_user_input` 入口对 `text.startswith('/')` 直接委托常驻 session，不经过 `_route()` |
| 16 | **Full 结果持久化规则（P0: FULL_RESULT_PERSISTENCE）** | Full 模式仅最终 summary 写入 session 历史；中间日志显示但不持久化；Full 模式不调 `self._session.send()` |

---

