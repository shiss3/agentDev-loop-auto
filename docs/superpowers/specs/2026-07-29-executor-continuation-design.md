# 指定执行器续作设计

日期：2026-07-29
状态：已批准

## 背景

交付轨执行器 = 一次性 `claude -p` 子进程（`executor.py:spawn_executor`），每模块每 attempt spawn 一次，merge 后 worktree 删除。无 session 持久化、无 resume 机制。

需求：模块 b 交付后，用户后续有同模块小任务时，想复用该模块执行器的对话上下文（模型记得做过什么），在输入框指定执行器直接续作，跳过需求解析。

## 目标

用户输入 `@<module_id> <提示词>`（module_id 须已交付过），跳过 `_parse_requirement`，进交付轨合成单模块 spec，`claude -p --resume <session_id>` 接续该模块执行器会话执行，走完整交付流（worktree → 执行 → merge → 验证）。

## 已决方案

- 输入语法：`@模块名 提示词`（否决斜杠命令：长提示词不自然）
- 上下文复用：`--resume session_id`（否决重注入：非真对话上下文；跨 cwd 可行性 spike 验证，失败才降级）
- 注册表：`.claude/executors.json`，键=module_id，同名新交付覆盖旧

## 组件改动

### 1. `src/autoloop_agent/core/executor_registry.py`（新建）

- 注册表：`.claude/executors.json`：`{"<module_id>": {"session_id": str, "req_id": str, "updated_at": iso str}}`。
- `load_registry(project_dir) -> dict` / `register_executor(project_dir, module_id, session_id, req_id) -> None`（同名覆盖）/ `lookup(project_dir, module_id) -> dict | None`。
- `extract_session_id(log_path) -> str | None`：解析 executor 日志首行 stream-json init（`{"type":"system","subtype":"init","session_id":...}`），逐行扫直到命中。

### 2. 注册时机（architect.py `_run_executors`）

模块 merge 成功（`✅ 模块 {mid} 交付完成` 分支）后：`extract_session_id` 该模块最后一次 attempt 日志 → `register_executor`。解析失败仅警告不阻塞交付。

### 3. 输入分流（repl.py `_on_user_input`）

现有 answer 模式判断之后、斜杠命令之前加分支：

- 正则 `^@(\S+)\s+(.+)` 匹配 → `governor.lookup_executor(name)`：
  - 命中：`_drawer.add_item(text, "continuation")`（item 记 `target=name`）入交付队列。
  - 未命中：主区提示"执行器 @name 未注册（未交付过该模块），按普通需求处理"，落回现有流程。
- 不匹配（无空格提示词、非 @ 开头）：原流程不动。

### 4. 续作交付路径（architect.py）

- Governor 新增公开入口 `continuation_flow(module_id, prompt, session_id)`：
  - 合成单模块 spec：`modules=[{module_id, summary: prompt, acceptance: [], subtasks: [{id: "follow-1", summary: prompt, intended_files: [], acceptance: []}], deps: []}]`，新 req_id，走现有 `_run_delivery`（分层/校验/merge/验证全复用）。
  - 把 `session_id` 传到 `_run_module_slot`（经 spec 或参数），spawn 用 resume 变体。
- repl `_run_delivery_item` 加 `kind == "continuation"` 分支：跳过 parse，state 直接 executing，调 `continuation_flow`。
- `executor.py:build_executor_args(module_prompt, max_turns, *, resume_session_id=None)`：命中时 argv 追加 `--resume <id>`。
- 续作 prompt = 用户原文 + 摘要协议（新 worktree 无摘要文件，executor 按协议重建 `summary-<mid>.md`，完成判定 `follow-1` [x] 照旧）。

### 5. 技术风险：跨 cwd resume（计划 Task 0 spike 验证）

session 建于已删除的旧 worktree 路径，续作在新 worktree（`deliver-<req_id>-<mid>`，路径不同）。`claude -p --resume <id>` 跨 cwd 是否可用未验证：

- spike 实测通过 → 按上述实现。
- spike 失败 → 降级方案：不 resume，把该模块历史 dispatch manifest + 交付摘要拼进 prompt 重注入（只改 `build_executor_args` 调用处，其余不变）。降级决定回到用户确认后实施。

### 6. resume 与服务端缓存

resume 价值在上下文记忆，非省 token：TTL（默认 5min）内前缀逐字一致才命中 cache_read；续作场景多超 TTL，首次请求吃全量历史 token，后续轮次恢复正常。执行器模型 glm-5.2 的缓存策略以服务商为准。

## 数据流

```
用户输入 @auth 把错误码改中文
  → repl 正则命中 + 注册表命中
  → 交付队列 item(kind=continuation, target=auth)
  → Governor.continuation_flow: 合成单模块 spec -> _run_delivery
  → 新 worktree -> build_executor_args(resume_session_id) -> claude -p --resume
  → 摘要协议判定完成 -> commit -> 验证 -> merge
  → 注册表更新(新 session_id 覆盖)
  → 抽屉事件流照旧可视化
```

## 错误处理

- @name 未注册：主区提示 + 按普通需求处理（不静默吞）。
- 日志无 session_id（init 行缺失）：该模块不注册，仅抽屉警告，交付不阻塞。
- resume 执行器失败（spawn 异常/超时未完成）：复用现有 slot 3 次 attempt + 失败保留 worktree 语义。
- 注册表 JSON 损坏：load 失败按空表处理（全部未注册），不 crash。

## 测试

全 mock，不拉真 claude CLI：

1. `extract_session_id`：合法日志命中 / 无 init 行返 None / 多行日志只取首个。
2. 注册表：register/lookup/同名覆盖/JSON 损坏容错。
3. @ 分流三态：命中入队 kind=continuation / 未命中提示+落回 / 非 @ 不干扰。
4. 合成 spec 结构（module_id/subtasks follow-1/deps 空）。
5. `build_executor_args(resume_session_id=...)`：argv 含 `--resume <id>`；None 时无。
6. merge 成功后注册表更新（mock extract/register）。
7. spike（手动）：跨 cwd resume 实测记录结论。

## 不做（YAGNI）

- 执行器列表/注销/重命名命令。
- 多 session 历史保留（同名覆盖）。
- resume 失败自动降级重注入（spike 挂了才做，需用户确认）。
- TTL 内缓存命中优化。
