# ClaudeAgentOptions 全字段（按用途分组）

> 源码: `types.py:1578`，dataclass，共 37 字段。下表「Phase 2」列: ✅直接用 / 🅿️P2-P3 启用 / ⬜不用或 CLI 子进程路径不经过 SDK。

## 执行与模型

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `model` | `str \| None` | None | ✅ | 模型 ID，如 `"claude-sonnet-5"`；None 走 CLI 默认 |
| `fallback_model` | `str \| None` | None | ⬜ | 主模型失败时的回退模型 |
| `cwd` | `str \| Path \| None` | None | ✅ | 会话工作目录；None 用进程 cwd |
| `cli_path` | `str \| Path \| None` | None | ⬜ | CLI 可执行文件路径；None 用 bundled |
| `env` | `dict[str,str]` | `{}` | ⬜ | 传给 CLI 子进程的环境变量（`CLAUDE_AGENT_SDK_CLIENT_APP` 标识本应用） |
| `extra_args` | `dict[str,str\|None]` | `{}` | ⬜ | 追加 CLI 参数（键不带 `--`，`None` 表示 bool flag） |
| `max_turns` | `int \| None` | None | 🅿️ | 回合上限 |
| `thinking` | `ThinkingConfig \| None` | None | ⬜ | `{"type":"adaptive"}` / `{"type":"enabled","budget_tokens":N}` / `{"type":"disabled"}` |
| `effort` | `EffortLevel \| None` | None | ⬜ | `low`/`medium`/`high`/`xhigh`(Opus 4.7)/`max` |
| `betas` | `list[SdkBeta]` | `[]` | 🅿️ | 仅 `"context-1m-2025-08-07"`（1M 上下文，Sonnet 4/4.5） |

## 工具与权限（★ Phase 2 核心）

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `tools` | `list[str] \| ToolsPreset \| None` | None | ✅ | **基础工具集（模型可见哪些工具）**。`[]` 禁用所有内置工具；`{"type":"preset","preset":"claude_code"}` 全默认 |
| `allowed_tools` | `list[str]` | `[]` | ✅ | **自动允许（不弹权限）**的工具名。注意: 不限制可见性，只跳过权限询问 |
| `disallowed_tools` | `list[str]` | `[]` | ⬜ | 从模型上下文移除的工具（彻底不可用） |
| `permission_mode` | `PermissionMode \| None` | None | ✅ | `default`/`acceptEdits`/`plan`/`bypassPermissions`/`dontAsk`/`auto`（见 [permissions.md](permissions.md)） |
| `can_use_tool` | `CanUseTool \| None` | None | ✅ | **动态权限回调**（见 [permissions.md](permissions.md)）；仅当 CLI 权限规则判定为 "ask" 时触发 |
| `permission_prompt_tool_name` | `str \| None` | None | ⬜ | 把权限请求路由到指定 MCP 工具 |
| `sandbox` | `SandboxSettings \| None` | None | ⬜ | 沙箱（命令执行的文件/网络隔离） |

> **⚠️ 核对发现（`tools` vs `allowed_tools`）**: 架构师 agent.md §5 路由判定用 `allowed_tools=[]`。但 `allowed_tools=[]` 只是「没有工具被自动允许」，工具仍对模型可见--模型仍可能尝试调用，触发 `can_use_tool`（未设）或 CLI 权限询问。若要**彻底禁工具**应用 `tools=[]`。路由判定 prompt + `output_format` 已约束模型只输出 JSON，`allowed_tools=[]` 实践上够用，但严格意义上 `tools=[]` 更稳妥。这是设计文档可复核的点。

## 系统提示与会话

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `system_prompt` | `str \| SystemPromptPreset \| SystemPromptFile \| None` | None | ✅ | 自定义 system prompt；`{"type":"preset","preset":"claude_code"}` 用默认；preset 可带 `append` |
| `setting_sources` | `list[SettingSource] \| None` | None | ⬜ | `user`/`project`/`local`；`[]` 禁用文件系统配置（SDK 隔离）；须含 `project` 才加载 CLAUDE.md |
| `settings` | `str \| None` | None | ⬜ | 额外 settings JSON 路径（flag settings 层，最高优先级） |
| `add_dirs` | `list[str\|Path]` | `[]` | ⬜ | cwd 之外允许访问的目录（绝对路径） |
| `resume` | `str \| None` | None | 🅿️ | 续接的 session_id（与 `continue_conversation` 互斥） |
| `continue_conversation` | `bool` | False | ⬜ | 续接当前目录最近会话 |
| `session_id` | `str \| None` | None | 🅿️ | 指定 session UUID；与 resume/continue 互斥（除非 `fork_session=True`） |
| `fork_session` | `bool` | False | 🅿️ | resume 时分叉到新 session_id |
| `user` | `str \| None` | None | ⬜ | 会话关联的用户标识 |

## 输出与流

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `output_format` | `dict \| None` | None | ✅ | **结构化输出**: `{"type":"json_schema","schema":{...}}`（见 [output-and-messages.md](output-and-messages.md)） |
| `include_partial_messages` | `bool` | False | ⬜ | 流式 partial（`SDKPartialAssistantMessage`） |
| `include_hook_events` | `bool` | False | ⬜ | 把 hook 事件作为 `HookEventMessage` 发入消息流 |

## 预算与检查点

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `max_budget_usd` | `float \| None` | None | 🅿️ | USD 上限，超限返回 `error_max_budget_usd` |
| `task_budget` | `TaskBudget \| None` | None | 🅿️ | API 侧 token 预算，`TaskBudget(total=N)`；带 `task-budgets-2026-03-13` beta header |
| `enable_file_checkpointing` | `bool` | False | 🅿️ | 开启后可用 `rewind_files()` 回滚 |

## MCP / Subagent / Plugin / Skills / Hooks

> 详见 [permissions.md](permissions.md) / [subagents.md](subagents.md) / [mcp.md](mcp.md) / [plugins.md](plugins.md) / [skills.md](skills.md) / [hooks.md](hooks.md)

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `mcp_servers` | `dict[str, McpServerConfig] \| str \| Path` | `{}` | 🅿️ | MCP server 配置（键=server名）；也可传 `.mcp.json` 路径 |
| `strict_mcp_config` | `bool` | False | ⬜ | True=只用此处传入的 MCP server，忽略其他来源 |
| `agents` | `dict[str, AgentDefinition] \| None` | None | ⬜ | 原生 subagent 定义（见 [subagents.md](subagents.md)） |
| `plugins` | `list[SdkPluginConfig]` | `[]` | ⬜ | 本地插件（见 [plugins.md](plugins.md)） |
| `skills` | `list[str] \| Literal["all"] \| None` | None | ⬜ | 启用 skills（见 [skills.md](skills.md)） |
| `hooks` | `dict[HookEvent, list[HookMatcher]] \| None` | None | 🅿️ | Hook 回调（见 [hooks.md](hooks.md)） |

## 会话存储

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `session_store` | `SessionStore \| None` | None | 🅿️ | 外部会话存储适配器（见 [sessions.md](sessions.md)） |
| `session_store_flush` | `SessionStoreFlushMode` | `"batched"` | 🅿️ | `batched`(默认)/`eager` |
| `load_timeout_ms` | `int` | 60000 | 🅿️ | `session_store.load()` 超时 |
