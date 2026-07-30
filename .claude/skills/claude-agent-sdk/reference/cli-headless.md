# Claude Code CLI Headless 标志核对（Full mode 用）

> 核对来源: `claude --help`（当前安装版本）+ SDK `subprocess_cli.py:221-410` (`_build_command`) 命令构造
> 核对日期: 2026-07-05
> 用途: L0 Full mode `_spawn_cli` 自己拼 `claude -p` 命令时，只信任本表确认存在的 flag。

## `claude --help` 确认存在的 headless 标志

| flag | 取值 / 用法 | SDK 映射来源 |
|------|------------|--------------|
| `-p, --print` | 非交互模式，print response and exit | Full mode `_spawn_cli` 必带 |
| `--allowedTools <tools...>` | 逗号或空格分隔，如 `"Bash(git *) Edit"` | `ClaudeAgentOptions.allowed_tools` -> `subprocess_cli.py:257` |
| `--disallowedTools <tools...>` | 同上 | `disallowed_tools` -> `:266` |
| `--tools <tools...>` | `""` 禁用所有 / `default` 全用 / `"Bash,Edit,Read"` 指定 | `tools` -> `:241-250` |
| `--output-format <format>` | `text` / `json` / `stream-json`（仅 `--print`） | SDK 固定 `stream-json` (`:225`) |
| `--input-format <format>` | `text` / `stream-json`（仅 `--print`） | SDK 固定 `stream-json` (`:408`) |
| `--permission-mode <mode>` | `acceptEdits`/`auto`/`bypassPermissions`/`manual`/`dontAsk`/`plan` | `permission_mode` -> `:286` |
| `--model <model>` | 别名 `fable`/`opus`/`sonnet` 或全名 `claude-fable-5` | `model` -> `:272` |
| `--system-prompt <prompt>` | 系统 prompt | `system_prompt` (str) -> `:230` |
| `--append-system-prompt <prompt>` | 追加到默认 prompt | preset+append -> `:237` |
| `--add-dir <dirs...>` | 额外允许目录 | `add_dirs` -> `:305` |
| `--settings <file-or-json>` | 额外 settings | `settings` -> `:300` |
| `--setting-sources <sources>` | `user,project,local` | `setting_sources` -> `:353` |
| `--mcp-config <configs...>` | MCP server JSON 文件或字符串 | `mcp_servers` -> `:326-332` |
| `--strict-mcp-config` | 只用 `--mcp-config` 的 server | `strict_mcp_config` -> `:341` |
| `--max-budget-usd <amount>` | USD 上限（仅 `--print`） | `max_budget_usd` -> `:263` |
| `--json-schema <schema>` | 结构化输出 schema | `output_format` -> `:404` |
| `--betas <betas...>` | beta headers（API key 用户） | `betas` -> `:278` |
| `--effort <level>` | `low`/`medium`/`high`/`xhigh`/`max` | `effort` -> `:393` |
| `--resume <id>` / `-r` | 续接 session | `resume` -> `:292` |
| `--session-id <uuid>` | 指定 session UUID | `session_id` -> `:295` |
| `--fork-session` | resume 时分叉 | `fork_session` -> `:344` |
| `--continue` / `-c` | 续接最近会话 | `continue_conversation` -> `:289` |
| `--include-partial-messages` | partial 消息（仅 print + stream-json） | `include_partial_messages` -> `:335` |
| `--include-hook-events` | hook 事件入流（仅 stream-json） | `include_hook_events` -> `:338` |
| `--no-session-persistence` | 不持久化（仅 print） | 通过 `extra_args` 注入 |
| `--plugin-dir <path>` | 本地插件 | `plugins` -> `:359` |
| `--worktree [name]` / `-w` | 创建 git worktree | 星射线用 Python 层自管 worktree，不用此 flag |
| `--verbose` | verbose 模式 | SDK 固定带 (`:225`) |
| `--dangerously-skip-permissions` | 绕过所有权限（沙箱用） | Full mode 备选，非默认 |

## `claude --help` 中**不存在**的 flag（SDK 仍在传 - Step0 已实测全部接受）

| flag | SDK 传的位置 | Step0 实测 | 备注 |
|------|-------------|-----------|------|
| `--max-turns` | `subprocess_cli.py:260` | ✅ exit=0 接受 | `ClaudeAgentOptions.max_turns` 映射；标准 5 行输出 |
| `--task-budget` | `:269` | ✅ exit=0 接受 | `task_budget` 映射（带 beta header）；标准 5 行输出 |
| `--session-mirror` | `:347` | ✅ exit=0 接受 | `session_store` 映射；输出 165 行（含 transcript mirror 事件） |
| `--thinking` | `:377-386` | ✅ exit=0 接受 | `thinking` 配置映射；输出 118 行（含 thinking 块） |
| `--system-prompt-file` | `:234` | ⏳ 未单独测 | `system_prompt` file 形式映射；同批隐藏 flag，推测同样接受 |
| `--permission-prompt-tool` | `:282` | ⏳ 未单独测 | `permission_prompt_tool_name` 映射；同批隐藏 flag，推测同样接受 |
| `--cwd` | **SDK 不传** | ❌ CLI 无此 flag | 工作目录走 subprocess `cwd=` 参数 |

> ✅ Step0 Spike 实测（2026-07-05，CLI v2.1.201）：`--max-turns`/`--task-budget`/`--session-mirror`/`--thinking` 四个 flag 带 `--verbose` 跑均 exit=0 被接受，是**隐藏 flag**（CLI 接受但不文档化）。
> ⚠️ **关键**：这四个 flag 必须配合 `--verbose` 才能跑通 -- 不带 `--verbose` 时 `-p --output-format stream-json` 直接 exit=1（与 flag 本身无关，是 stream-json 对 `--verbose` 的强制依赖，见下文「Full mode `_spawn_cli` 正确命令模板」）。
> ✅ `--cwd` 确认不存在 -- Full mode `_spawn_cli` 已改用 `create_subprocess_exec(cwd=worktree)`。

## `PermissionMode` SDK vs CLI 差异

| | SDK `PermissionMode` (`types.py:24`) | CLI `--permission-mode` choices (`claude --help`) |
|---|---|---|
| 共有 | `acceptEdits` / `plan` / `bypassPermissions` / `dontAsk` / `auto` | 同 |
| 差异 | `default` | `manual` |

- SDK 的 `default` = 不传 `--permission-mode` flag（走 CLI 默认行为）。
- CLI 的 `manual` 在 SDK 中无对应（可能 SDK 用 `default` 涵盖，或 `manual` 是 CLI 交互模式专用）。
- Full mode `_spawn_cli` 若需指定，用 `--permission-mode acceptEdits`（子进程在隔离 worktree，安全）。

## Full mode `_spawn_cli` 正确命令模板（Step0 实测后）

```python
proc = await asyncio.create_subprocess_exec(
    "claude", "-p", task["prompt"],
    "--tools", "Read,Write,Edit,Bash,Glob,Grep",   # L2 可写白名单；L0/L1 只读用 "--tools","Read,Glob,Grep"
    "--permission-mode", "acceptEdits",            # worktree 隔离下安全；-p 模式不弹窗
    "--output-format", "stream-json",
    "--verbose",                                   # Step0 实测：必需！不带则 exit=1
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=worktree,                                  # CLI 无 --cwd flag，通过 subprocess 参数设置
)
```

关键点（Step0 实测后）:
1. **`cwd=worktree`**（不是 `--cwd` flag）-- CLI 无 `--cwd`；Step0 实测 CLI 读 cwd/.claude 配置，worktree 隔离下子进程读各自项目配置。
2. **`--tools`（非 `--allowedTools`）** -- Step0 实测两者语义不同：`--tools` 是**可见工具白名单**（白名单外工具对模型不可见，模型不调用）；`--allowedTools` 是**自动允许列表**（工具仍可见，调用被拒记入 `permission_denials`，多耗 1 turn + ~2× cost）。L0/L1 只读约束必须用 `--tools`。
3. **`--permission-mode acceptEdits`** -- worktree 隔离下安全；`-p` 模式权限拒绝正常 exit=0 记入 `permission_denials`，不卡死（Step0 实测）。
4. **`--verbose` 必需** -- Step0 实测：`-p --output-format stream-json` 不带 `--verbose` 则 exit=1（`requires --verbose`）；带后 stderr 空、不污染 stdout。
5. **prompt 作为位置参数** -- `-p` 模式下 prompt 作位置参数；`create_subprocess_exec` 不走 shell，参数转义安全。

## stream-json 输出结构（Step0 已抓取 2026-07-05）

Step0 实测：`-p --output-format stream-json --verbose` 输出 JSON Lines，每行一个事件对象。实测事件类型分布（一次含工具调用的完整 run）:

| type | 含义 | 数量(示例) |
|------|------|-----------|
| `system` | hook_started / hook_response / init 事件 | 大量（权限检查触发 hook） |
| `assistant` | 模型消息（content: TextBlock / ToolUseBlock / thinking） | 每轮 1-2 |
| `user` | 工具结果回填（tool_result） | 每次工具调用 1 |
| `result` | **结算消息**（run 终止） | **恰好 1 行（最后一行）** |

**`result` 行字段路径（解析依据）**:
```json
{
  "type": "result",
  "subtype": "success",              // 错误时为 error_* (如 error_max_turns)
  "is_error": false,                 // bool，错误判定
  "result": "最终文本",               // ✅ _extract_result_text() 取此字段（字符串，非 content[0].text）
  "total_cost_usd": 0.093,           // ✅ _extract_cost() 取此字段
  "usage": {
    "input_tokens": 11089,           // ✅ _extract_usage() 取此
    "output_tokens": 689,
    "cache_read_input_tokens": 40640
  },
  "num_turns": 2,
  "duration_ms": 25188,
  "session_id": "...",               // 可用于续接
  "permission_denials": [            // ✅ 被拒工具记录（--allowedTools 模式下非空，--tools 模式为空）
    {"tool_name": "Write", "tool_use_id": "...", "tool_input": {...}}
  ],
  "modelUsage": {"ark-code-latest": {"inputTokens":..., "costUSD":..., "contextWindow":200000}},
  "uuid": "..."
}
```

**`system.init` 行（第 3 行左右）含运行环境信息**:
```json
{"type":"system","subtype":"init","cwd":"...","tools":[...],"mcp_servers":[...],"model":"ark-code-latest","permissionMode":"default",...}
```

> ✅ 解析策略：逐行 `json.loads`，按 `type=="result"` 过滤取最后一行；从该行提取 `result` / `total_cost_usd` / `usage` / `is_error` / `subtype` / `permission_denials`。
> ⚠️ `result` 行的最终文本是**顶层 `result` 字符串字段**，**不**是 `message.content[0].text`（后者是 `assistant` 行的结构）。
