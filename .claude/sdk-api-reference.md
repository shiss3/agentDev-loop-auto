# claude-agent-sdk 能力与 API 速查（星射线 Phase 2 视角）

> **SDK 版本**: v0.2.93
> **核对来源**: `.venv/Lib/site-packages/claude_agent_sdk/` 源码逐字段核对（非记忆）
> **核对日期**: 2026-07-05
> **范围**: 只收项目会使用到的能力。Phase 2 直接用的给「签名 + 字段 + 落地」；Phase 2 P2/P3 启用的给「签名 + 何时启用」；不用的明确标注并说明原因。
> **硬依赖**: `mcp` 包。`__init__.py:19` 顶部 `from mcp.types import ToolAnnotations` 是模块级硬导入——未安装 `mcp` 包时整个 SDK 无法 import。`create_sdk_mcp_server()` 内部还 `from mcp.server import Server`（函数级导入）。

---

## 0. 两个执行入口（最关键的选择）

SDK 提供两个入口，星射线两个都用。

### 0.1 `query()` — 一次性 / 无状态 / 单向

> 源码: `query.py:11`

```python
async def query(
    *,
    prompt: str | AsyncIterable[dict[str, Any]],
    options: ClaudeAgentOptions | None = None,
    transport: Transport | None = None,
) -> AsyncIterator[Message]
```

- **单向**: 所有 prompt 先发完，再收所有响应；不能中途追问 / 不能中断。
- **无状态**: 每次 query 独立，不持有会话状态（除非 `resume` / `continue_conversation`）。
- **星射线落地**: **L0 路由判定**用这个（ADR-14）。一次 LLM 调用产出 fast/full 分类，不新建 `BaseAgentSession`、不持久化、不挂 `can_use_tool`。

### 0.2 `ClaudeSDKClient` — 有状态 / 交互式 / 双向

> 源码: `client.py:67` (`__init__`)，方法清单见下

```python
client = ClaudeSDKClient(options=None, transport=None)
async with client:  # __aenter__ / __aexit__ (client.py:619/624)
    await client.connect(prompt=None)          # client.py:99
    await client.query(prompt, session_id="default")  # client.py:283
    async for msg in client.receive_messages(): ...    # client.py:271
    await client.interrupt()                   # client.py:313
    await client.disconnect()                  # client.py:608
```

`ClaudeSDKClient` 公开方法（`client.py` 全量）:

| 方法 | 行 | 用途 |
|------|----|------|
| `__init__(options, transport)` | 67 | 构造，options 为 None 时用默认 `ClaudeAgentOptions()` |
| `connect(prompt=None)` | 99 | 启动 CLI 子进程并建立连接；prompt 可为 str / AsyncIterable / None |
| `receive_messages()` | 271 | `AsyncIterator[Message]`，主消息流 |
| `query(prompt, session_id="default")` | 283 | 在已连接会话中追加一轮用户消息 |
| `interrupt()` | 313 | 中断当前回合（仅流式模式） |
| `set_permission_mode(mode)` | 319 | 运行时切换权限模式 |
| `set_model(model=None)` | 346 | 运行时切换模型 |
| `rewind_files(user_message_id)` | 370 | 文件回滚到某 user message 时的状态（需 `enable_file_checkpointing`） |
| `reconnect_mcp_server(name)` | 402 | 重连指定 MCP server |
| `toggle_mcp_server(name, enabled)` | 424 | 启用/禁用 MCP server |
| `stop_task(task_id)` | 450 | 停止子任务 |
| `get_mcp_status()` | 473 | → `McpStatusResponse` |
| `get_context_usage()` | 506 | → `ContextUsageResponse`（**ContextGuard 用**） |
| `get_server_info()` | 542 | → server 信息 dict |
| `receive_response()` | 567 | `AsyncIterator[Message]`，收单轮响应 |
| `disconnect()` | 608 | 关闭连接 |

- **星射线落地**: **L0 Fast Lane 常驻会话**用 `ClaudeSDKClient`（已在 `chat/session.py` 的 `ChatSession` 里封装）。Fast Lane 走 `client.query(text)` 追加对话、`receive_messages()` 流式渲染、`interrupt()` 实现 `cancel()`。

---

## 1. `ClaudeAgentOptions` 全字段（按用途分组）

> 源码: `types.py:1578`，dataclass，共 37 字段。下表「Phase 2」列: ✅直接用 / 🅿️P2-P3 启用 / ⬜不用或 CLI 子进程路径不经过 SDK。

### 1.1 执行与模型

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

### 1.2 工具与权限（★ Phase 2 核心）

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `tools` | `list[str] \| ToolsPreset \| None` | None | ✅ | **基础工具集（模型可见哪些工具）**。`[]` 禁用所有内置工具；`{"type":"preset","preset":"claude_code"}` 全默认 |
| `allowed_tools` | `list[str]` | `[]` | ✅ | **自动允许（不弹权限）**的工具名。注意: 不限制可见性，只跳过权限询问 |
| `disallowed_tools` | `list[str]` | `[]` | ⬜ | 从模型上下文移除的工具（彻底不可用） |
| `permission_mode` | `PermissionMode \| None` | None | ✅ | `default`/`acceptEdits`/`plan`/`bypassPermissions`/`dontAsk`/`auto`（见 §2） |
| `can_use_tool` | `CanUseTool \| None` | None | ✅ | **动态权限回调**（见 §3）；仅当 CLI 权限规则判定为 "ask" 时触发 |
| `permission_prompt_tool_name` | `str \| None` | None | ⬜ | 把权限请求路由到指定 MCP 工具 |
| `sandbox` | `SandboxSettings \| None` | None | ⬜ | 沙箱（命令执行的文件/网络隔离） |

> **⚠️ 核对发现（`tools` vs `allowed_tools`）**: 架构师 agent.md §5 路由判定用 `allowed_tools=[]`。但 `allowed_tools=[]` 只是「没有工具被自动允许」，工具仍对模型可见——模型仍可能尝试调用，触发 `can_use_tool`（未设）或 CLI 权限询问。若要**彻底禁工具**应用 `tools=[]`。路由判定 prompt + `output_format` 已约束模型只输出 JSON，`allowed_tools=[]` 实践上够用，但严格意义上 `tools=[]` 更稳妥。这是设计文档可复核的点。

### 1.3 系统提示与会话

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

### 1.4 输出与流

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `output_format` | `dict \| None` | None | ✅ | **结构化输出**: `{"type":"json_schema","schema":{...}}`（见 §4） |
| `include_partial_messages` | `bool` | False | ⬜ | 流式 partial（`SDKPartialAssistantMessage`） |
| `include_hook_events` | `bool` | False | ⬜ | 把 hook 事件作为 `HookEventMessage` 发入消息流 |

### 1.5 预算与检查点

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `max_budget_usd` | `float \| None` | None | 🅿️ | USD 上限，超限返回 `error_max_budget_usd` |
| `task_budget` | `TaskBudget \| None` | None | 🅿️ | API 侧 token 预算，`TaskBudget(total=N)`；带 `task-budgets-2026-03-13` beta header |
| `enable_file_checkpointing` | `bool` | False | 🅿️ | 开启后可用 `rewind_files()` 回滚 |

### 1.6 MCP / Subagent / Plugin / Skills / Hooks（见 §3-§8）

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `mcp_servers` | `dict[str, McpServerConfig] \| str \| Path` | `{}` | 🅿️ | MCP server 配置（键=server名）；也可传 `.mcp.json` 路径 |
| `strict_mcp_config` | `bool` | False | ⬜ | True=只用此处传入的 MCP server，忽略其他来源 |
| `agents` | `dict[str, AgentDefinition] \| None` | None | ⬜ | 原生 subagent 定义（见 §5） |
| `plugins` | `list[SdkPluginConfig]` | `[]` | ⬜ | 本地插件（见 §7） |
| `skills` | `list[str] \| Literal["all"] \| None` | None | ⬜ | 启用 skills（见 §8） |
| `hooks` | `dict[HookEvent, list[HookMatcher]] \| None` | None | 🅿️ | Hook 回调（见 §4.4） |

### 1.7 会话存储

| 字段 | 类型 | 默认 | Phase 2 | 说明 |
|------|------|------|---------|------|
| `session_store` | `SessionStore \| None` | None | 🅿️ | 外部会话存储适配器（见 §4.1） |
| `session_store_flush` | `SessionStoreFlushMode` | `"batched"` | 🅿️ | `batched`(默认)/`eager` |
| `load_timeout_ms` | `int` | 60000 | 🅿️ | `session_store.load()` 超时 |

---

## 2. `PermissionMode` 取值

> 源码: `types.py:24`

```python
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
```

| 值 | 行为 |
|----|------|
| `default` | 标准行为，危险操作弹权限 |
| `acceptEdits` | 自动接受文件编辑 |
| `plan` | 计划模式，不执行工具 |
| `bypassPermissions` | 绕过所有权限检查 |
| `dontAsk` | 不弹窗，未预批准即拒绝 |
| `auto` | 模型分类器决定每次工具调用允许/拒绝 |

> 注: `ClaudeAgentOptions.permission_mode` 的 docstring（`types.py:1629`）只列了前 5 个，但 `types.py:24` 的 Literal 和 `query.py` docstring 都含 `auto`。以 Literal 定义为准。

---

## 3. 动态权限: `can_use_tool` + `PermissionResult`（★ Phase 2 核心）

> 源码: `types.py:197-255`

### 3.1 回调签名

```python
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],   # (tool_name, tool_input, context)
    Awaitable[PermissionResult]
]
```

仅当 CLI 权限规则判定为 "ask" 时触发；`allowed_tools` / `permission_mode=acceptEdits|bypassPermissions` / settings 的 `permissions.allow` 已批准的工具**不会**触发此回调。要观察/拦截**每一次**工具调用应改用 `PreToolUse` hook。

### 3.2 `ToolPermissionContext` 字段（`types.py:198`）

`signal` / `suggestions` / `tool_use_id` / `agent_id`(sub-agent 内) / `blocked_path` / `decision_reason` / `title`(完整权限句) / `display_name`(短标签) / `description`(副标题)

### 3.3 返回值

```python
@dataclass
class PermissionResultAllow:                       # types.py:234
    behavior: Literal["allow"] = "allow"
    updated_input: dict | None = None              # 改写后的工具入参
    updated_permissions: list[PermissionUpdate] | None = None

@dataclass
class PermissionResultDeny:                        # types.py:242
    behavior: Literal["deny"] = "deny"
    message: str = ""
    interrupt: bool = False                        # True=同时中断整个会话

PermissionResult = PermissionResultAllow | PermissionResultDeny
```

### 3.4 星射线落地

- **Fast Lane**: `can_use_tool` 返回 `PermissionResultAllow(behavior="allow")`——Fast Lane 在常驻会话里有写权限，全放行。
- **Full mode**: **不走 SDK 的 `can_use_tool`**。Full mode 用 `claude -p` CLI 子进程（独立 worktree），权限通过 CLI flag（`--allowedTools` / `--disallowedTools` / `--permission-mode`）控制——deny `Write`/`Edit`/`NotebookEdit`，allow `Bash`。SDK 的 `can_use_tool` 只对 SDK 进程内的会话生效，CLI 子进程不经过。

---

## 4. Phase 2 直接使用的能力

### 4.1 结构化输出: `output_format` → `ResultMessage.structured_output`（★ 路由判定）

> 源码: `ClaudeAgentOptions.output_format` (`types.py:1889`)，`ResultMessage.structured_output` (`types.py:1158`)

```python
options = ClaudeAgentOptions(
    output_format={"type": "json_schema", "schema": {
        "type": "object",
        "properties": {"lane": {"type": "string", "enum": ["fast", "full"]}},
        "required": ["lane"],
    }},
)
async for msg in query(prompt=ROUTE_JUDGE_PROMPT, options=options):
    if isinstance(msg, ResultMessage):
        decision = msg.structured_output   # {"lane": "fast"} 或 {"lane": "full"}
```

`ResultMessage` 完整字段（`types.py:1144`）: `subtype` / `duration_ms` / `duration_api_ms` / `is_error` / `num_turns` / `session_id` / `stop_reason` / `total_cost_usd` / `usage` / `result`(文本) / `structured_output` / `model_usage` / `permission_denials` / `deferred_tool_use` / `errors` / `api_error_status`(v2.1.110+, 429/500/529) / `uuid`。

> **⚠️ Full mode cost 解析**: L0 Full mode 用 CLI 子进程，**没有 `ResultMessage` 对象**，无法读 `total_cost_usd`。须从子进程 stream-json stdout 解析 cost 累加。这是架构师 agent.md §6.1 已标注的设计约束。

### 4.2 上下文用量: `get_context_usage()`

> 源码: `client.py:506`，`ContextUsageResponse` (`types.py:759`)

```python
usage = await client.get_context_usage()
# usage.percentage: float (0-100)         ← ContextGuard 驱动 kill/respawn
# usage.isAutoCompactEnabled: bool
# usage.totalTokens / usage.maxTokens / usage.rawMaxTokens
# usage.categories: list[ContextUsageCategory]  (system prompt / tools / messages ...)
```

**星射线落地**: `ContextGuard`（Phase 2 P2）用 `percentage` 驱动压缩/kill/respawn 决策。CLAUDE.md 速查表里标注此项「开箱即用」。

### 4.3 消息类型

> 源码: `types.py:920-1167`

| 类型 | 行 | 用途 |
|------|----|------|
| `UserMessage` | 1014 | 用户消息 |
| `AssistantMessage` | 1024 | 模型消息，`.content: list[ContentBlock]` |
| `TextBlock` | 920 | 文本块 (`text`) |
| `ToolUseBlock` | 935 | 工具调用块 (`id`, `name`, `input`) |
| `ToolResultBlock` | 945 | 工具结果块 |
| `ThinkingBlock` | — | 思考块 |
| `SystemMessage` | 1040 | 系统消息 |
| `ResultMessage` | 1144 | 结算消息（见 §4.1） |
| `StreamEvent` | 1170 | partial 流事件 (`event` 是原始 Anthropic API 事件) |
| `RateLimitEvent` | 1214 | 限流状态变化（`allowed_warning`/`rejected`） |

`Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | ...`（联合类型）。

---

## 5. subagent（原生子 Agent）

> 源码: `AgentDefinition` (`types.py:82`)，`ClaudeAgentOptions.agents` (`types.py:1794`)

### 5.1 `AgentDefinition` 字段

```python
@dataclass
class AgentDefinition:                              # types.py:82
    description: str                                # 必需
    prompt: str                                     # 必需
    tools: list[str] | None = None                  # 已弃用传 "Skill"，用 skills
    disallowedTools: list[str] | None = None
    model: str | None = None                        # "sonnet"/"opus"/"haiku"/"inherit" 或完整 model ID
    skills: list[str] | None = None                 # ← subagent 也能挂 skills
    memory: Literal["user","project","local"] | None = None
    mcpServers: list[str | dict[str, Any]] | None = None   # server 名或 inline 配置
    initialPrompt: str | None = None
    maxTurns: int | None = None
    background: bool | None = None
    effort: EffortLevel | int | None = None
    permissionMode: PermissionMode | None = None
```

### 5.2 用法

```python
options = ClaudeAgentOptions(agents={
    "code-reviewer": AgentDefinition(
        description="审查代码变更",
        prompt="你是一个代码审查员...",
        tools=["Read", "Grep", "Glob"],
        model="sonnet",
    ),
})
# 主模型通过内置 "Agent" 工具调用 "code-reviewer"，SDK 自动管理其执行循环
```

### 5.3 星射线落地说明（⚠️ 当前不用）

星射线 L0 Full mode 用 `claude -p` **CLI 子进程**（独立 worktree）跑领域任务，**不用** SDK 原生 `agents`。原因: CLI 子进程天然提供进程级隔离 + worktree 隔离 + 独立 stdout 解析，符合 Full mode 的并行合并设计。

SDK 原生 `agents` 是未来 L1→L2 委派的备选通道（若 L1/L2 改为进程内 subagent 而非 CLI 子进程时启用）。当前 Phase 2 不启用，但保留 `AgentDefinition` 定义供 Phase 3+ 评估。

---

## 6. MCP（Model Context Protocol）

> 源码: `__init__.py:307` (`create_sdk_mcp_server`)，`__init__.py:166` (`tool`)，`__init__.py:155` (`SdkMcpTool`)，`types.py:602-637` (配置类型)

### 6.1 创建进程内 MCP server

```python
@dataclass
class SdkMcpTool(Generic[T]):                       # __init__.py:155
    name: str
    description: str
    input_schema: type[T] | dict[str, Any]          # dict / TypedDict / JSON Schema
    handler: Callable[[T], Awaitable[dict[str, Any]]]
    annotations: ToolAnnotations | None = None

def tool(name, description, input_schema, annotations=None)  # __init__.py:166
    # 装饰器，返回 SdkMcpTool

def create_sdk_mcp_server(                          # __init__.py:307
    name: str, version: str = "1.0.0", tools: list[SdkMcpTool] | None = None
) -> McpSdkServerConfig
```

### 6.2 用法

```python
@tool("greet", "Greet a user", {"name": str})
async def greet(args):
    return {"content": [{"type": "text", "text": f"Hello, {args['name']}!"}]}

server = create_sdk_mcp_server(name="my-tools", tools=[greet])
options = ClaudeAgentOptions(
    mcp_servers={"my-tools": server},
    allowed_tools=["greet"],
)
```

- 进程内运行，无 IPC 开销，可直接访问应用状态。
- `tool` handler 必须是 async，接收单个 dict，返回 `{"content": [...], "is_error": bool}`。
- `input_schema` 支持三种: dict 映射 (`{"name": str}`) / TypedDict / 完整 JSON Schema。
- 内部 `from mcp.server import Server`（函数级导入），依赖 `mcp` 包。

### 6.3 四种 `McpServerConfig`（`types.py:602-637`）

| 类型 | 字段 | 说明 |
|------|------|------|
| `McpStdioServerConfig` | `command`, `args`, `env` | 子进程 stdio |
| `McpSSEServerConfig` | `url`, `headers` | SSE |
| `McpHttpServerConfig` | `url`, `headers` | HTTP |
| `McpSdkServerConfig` | `name`, `instance` | 进程内（`create_sdk_mcp_server` 返回） |

`ClaudeSDKClient.get_mcp_status()` → `McpStatusResponse`（`client.py:473`）。

### 6.4 星射线落地

GCH（全局上下文中枢）规划为 MCP server（`core/gch/server.py`）。但 **L0 Router 当前不挂 GCH**——L0 只做路由判定 + Fast Lane 执行。GCH 在 L1/L2 启用时通过 `mcp_servers={"gch": ...}` 挂载，L0/L1 读写全局记忆，L2 不挂载。当前 Phase 2 L0 阶段不启用，Phase 2 P0 后期或 Phase 3+ 评估。

---

## 7. 插件（`SdkPluginConfig`）

> 源码: `types.py:824`，`ClaudeAgentOptions.plugins` (`types.py:1844`)

```python
class SdkPluginConfig(TypedDict):
    type: Literal["local"]      # 目前仅支持 local
    path: str

options = ClaudeAgentOptions(plugins=[{"type": "local", "path": "/path/to/plugin"}])
```

插件提供自定义命令、agents、skills、hooks。**星射线当前不使用**——所有能力自建。保留备查。

---

## 8. Skills（★ 澄清: 是 SDK 原生能力，非 CLI 专属）

> 源码: `ClaudeAgentOptions.skills` (`types.py:1812`)，`AgentDefinition.skills` (`types.py:93`)

### 8.1 主会话 skills

```python
options = ClaudeAgentOptions(
    skills=["my-skill", "plugin:skill-name"]   # list[str]：只启用列出的
    # 或 skills="all"                            # 启用所有发现的 skill
    # 或 skills=None (默认)                      # 不做 SDK 自动配置，CLI 默认仍生效
    # 或 skills=[]                                # 抑制所有 skill
)
```

SDK 文档原文（`types.py:1812`）: *"This is the single place to turn skills on; you do not need to add `"Skill"` to `allowed_tools` or set `setting_sources` yourself — the SDK does both when this is set."*

- 名字匹配 `SKILL.md` 的 `name` / 目录名，插件 skill 用 `plugin:skill` 形式。
- **是上下文过滤器，不是沙箱**: 未列出的 skill 对模型隐藏、被 Skill 工具拒绝，但其文件仍在磁盘上，可被 `Read`/`Bash` 访问。不要在 skill 文件里存密钥。

### 8.2 subagent skills

`AgentDefinition.skills: list[str] | None`（`types.py:93`）——subagent 也能挂 skills。

### 8.3 `allowed_tools` 弃用说明

`ClaudeAgentOptions.allowed_tools` docstring（`types.py:1599`）: 传 `"Skill"` 已弃用，改用 `skills` 选项。`AgentDefinition.tools` 同理（`types.py:88`）。

### 8.4 星射线落地

Phase 4 的 Skill 知识库（`.skills/` + `SkillsAgent`）规划用此能力。当前 Phase 2 **不启用** skills。但核对结论修正了早期判断: **skills 是 SDK Python API 的原生能力**，不是只能通过 CLI 使用的概念。

---

## 9. 会话持久化与管理（Phase 2 P2 挂起池）

### 9.1 `SessionStore` Protocol

> 源码: `types.py:1370`

```python
class SessionStore(Protocol):
    async def append(self, key, entries: list[SessionStoreEntry]) -> None: ...      # 必需
    async def load(self, key) -> list[SessionStoreEntry] | None: ...                # 必需
    async def list_sessions(self, project_key) -> list[SessionStoreListEntry]: ...  # 可选
    async def list_session_summaries(self, project_key) -> list[SessionSummaryEntry]: ...  # 可选
    async def delete(self, key) -> None: ...                                        # 可选
    async def list_subkeys(self, key) -> list[str]: ...                             # 可选
```

- 子进程仍写本地磁盘（`CLAUDE_CONFIG_DIR=/tmp` 可走临时副本），adapter 收到**副**本。
- `append` 在子进程本地写成功**之后**调用——本地耐久性已保证。
- SDK 提供 `InMemorySessionStore`（`_internal/session_store.py`）。

### 9.2 模块级会话操作函数（`_internal/session_mutations.py`）

| 函数 | 签名 | 说明 |
|------|------|------|
| `fork_session` | `(session_id, directory=None, up_to_message_id=None, title=None) -> ForkSessionResult` | 分叉到新 session（fresh UUID），可指定分叉点 |
| `tag_session` | `(session_id, tag, directory=None) -> None` | 打标签；`tag=None` 清除 |
| `rename_session` | `(session_id, new_title, directory=None) -> None` | 重命名 |
| `delete_session` | `(session_id, directory=None) -> None` | 硬删除 JSONL + 子 agent 转录 |

每个都有 `*_via_store` async 变体（走 `SessionStore`）。`ForkSessionResult.session_id: str`（`session_mutations.py:233`）。

### 9.3 会话列举与读取（`_internal/sessions.py`）

`list_sessions(directory)` / `get_session_info(session_id)` / `get_session_messages(session_id)` / `list_subagents(session_id)` / `get_subagent_messages(...)`，及 `*_from_store` async 变体。返回 `SDKSessionInfo`（`types.py:1496`）/ `SessionMessage`（`types.py:1531`）。

### 9.4 星射线落地

挂起池（Phase 2 P2）: `core/pool.py` 用 SQLite 索引 + SDK `session_store`。挂起的 L2 session 通过 `fork_session(up_to_message_id=...)` 从检查点分叉并行尝试，互不污染；`tag_session` 标记挂起状态。

---

## 10. Hooks（Phase 2 P2 / Phase 3）

> 源码: `HookMatcher` (`types.py:584`)，`ClaudeAgentOptions.hooks` (`types.py:1760`)，Hook 输入类型 (`types.py:273+`)

### 10.1 `HookEvent` 取值（`types.py:259`）

`PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `UserPromptSubmit` / `Stop` / `SubagentStop` / `PreCompact` / `Notification` / `SubagentStart` / `PermissionRequest`

### 10.2 `HookMatcher`

```python
@dataclass
class HookMatcher:                                  # types.py:584
    matcher: str | None = None                      # 工具名，如 "Bash" 或 "Write|Edit|MultiEdit"
    hooks: list[HookCallback] = field(default_factory=list)
    timeout: float | None = None                    # 秒，默认 60
```

### 10.3 用法

```python
options = ClaudeAgentOptions(hooks={
    "PreToolUse": [HookMatcher(
        matcher="Write|Edit",
        hooks=[my_guard_callback],
    )],
})
```

**派发顺序**（`types.py:1766` 原文）: 同一事件上的多个 matcher **并发派发**，不保证顺序——每个 hook 须独立，不能依赖另一个先完成。

### 10.4 星射线落地

- `PreToolUse` → 危险工具护栏（`core/guard.py`，P2 预演）/ 代码审查拦截 / 沙箱回滚（Phase 3）
- `PreCompact` → 压缩前抢救耐久事实写入 GCH
- `SubagentStart`/`SubagentStop` → 编排进度可视化

---

## 11. 星射线 Phase 2 落地映射总表

| SDK 能力 | API | 星射线用途 | Phase 2 状态 |
|----------|-----|-----------|--------------|
| 一次性 query | `query()` | L0 路由判定（一次性、不持久化、无 can_use_tool） | ✅ Step 2 |
| 常驻会话 | `ClaudeSDKClient` | L0 Fast Lane 常驻（已在 `ChatSession`） | ✅ Step 1 |
| 结构化输出 | `output_format` / `ResultMessage.structured_output` | 路由判定 JSON 输出 | ✅ Step 2 |
| 动态权限 | `can_use_tool` / `PermissionResultAllow`/`Deny` | Fast Lane 全放行 | ✅ Step 2 |
| 上下文用量 | `ClaudeSDKClient.get_context_usage()` | ContextGuard kill/respawn | 🅿️ P2 |
| 消息类型 | `AssistantMessage`/`TextBlock`/`ToolUseBlock`/`ResultMessage` | 渲染 + cost 解析 | ✅ |
| 会话存储 | `SessionStore` / `InMemorySessionStore` | 挂起池底座 | 🅿️ P2 |
| 会话操作 | `fork_session` / `tag_session` | 挂起池分叉/标记 | 🅿️ P2 |
| 文件检查点 | `enable_file_checkpointing` / `rewind_files()` | 干净重试 | 🅿️ P2 |
| 预算 | `task_budget` / `max_budget_usd` | L2 子 Agent 成本上限 | 🅿️（Full mode CLI 子进程改从 stdout 解析） |
| Hooks | `PreToolUse` / `PreCompact` / `SubagentStart|Stop` | 护栏/抢救/可视化 | 🅿️ P2-P3 |
| 长上下文 | `betas=["context-1m-2025-08-07"]` | L0 全局视野 | 🅿️ P3 |
| 原生 subagent | `AgentDefinition` + `agents` | L1→L2 委派备选 | ⬜ 当前用 CLI 子进程替代 |
| MCP | `create_sdk_mcp_server` / `mcp_servers` | GCH 总线 | ⬜ L1/L2 启用时 |
| 插件 | `SdkPluginConfig` / `plugins` | — | ⬜ 不用 |
| Skills | `ClaudeAgentOptions.skills` / `AgentDefinition.skills` | Skill 知识库 | ⬜ Phase 4 |

---

## 12. 关键核对结论（修正项）

1. **skills 是 SDK 原生 Python API 能力**（`ClaudeAgentOptions.skills` + `AgentDefinition.skills`），不是只能通过 CLI 使用的概念。早期「skills 是 CLI 专属」的判断**作废**。
2. **`mcp` 包是 SDK 硬依赖**（`__init__.py:19` 顶部导入），不是可选依赖。项目须确保 `mcp` 已安装。
3. **`PermissionMode` 含 `auto`**（6 个值），`ClaudeAgentOptions.permission_mode` docstring 漏标，以 `types.py:24` Literal 为准。
4. **`tools` vs `allowed_tools` 区别**: `tools` 控制可见性（`[]` 彻底禁用），`allowed_tools` 只控制是否跳过权限询问。路由判定若要严格禁工具，`tools=[]` 比 `allowed_tools=[]` 更稳妥——架构师 agent.md §5 可复核此点。
5. **`create_sdk_mcp_server` / `tool` / `SdkMcpTool` 定义在 `__init__.py`**（非 `_sdk_mcp_server.py`，该文件不存在），`fork_session` / `tag_session` 在 `_internal/session_mutations.py`。
6. **Full mode 不经 SDK**: L0 Full mode 用 `claude -p` CLI 子进程，SDK 的 `can_use_tool` / `ResultMessage.total_cost_usd` / `output_format` 等对它**不生效**——权限走 CLI flag，cost 走 stdout 解析。SDK 能力只在 Fast Lane（常驻 SDK 会话）和路由判定（`query()` 顶层）路径生效。

---

## 附录 A: Claude Code CLI Headless 标志核对（Full mode 用）

> 核对来源: `claude --help`（当前安装版本）+ SDK `subprocess_cli.py:221-410` (`_build_command`) 命令构造
> 核对日期: 2026-07-05
> 用途: L0 Full mode `_spawn_cli` 自己拼 `claude -p` 命令时，只信任本表确认存在的 flag。

### A.1 `claude --help` 确认存在的 headless 标志

| flag | 取值 / 用法 | SDK 映射来源 |
|------|------------|--------------|
| `-p, --print` | 非交互模式，print response and exit | Full mode `_spawn_cli` 必带 |
| `--allowedTools <tools...>` | 逗号或空格分隔，如 `"Bash(git *) Edit"` | `ClaudeAgentOptions.allowed_tools` → `subprocess_cli.py:257` |
| `--disallowedTools <tools...>` | 同上 | `disallowed_tools` → `:266` |
| `--tools <tools...>` | `""` 禁用所有 / `default` 全用 / `"Bash,Edit,Read"` 指定 | `tools` → `:241-250` |
| `--output-format <format>` | `text` / `json` / `stream-json`（仅 `--print`） | SDK 固定 `stream-json` (`:225`) |
| `--input-format <format>` | `text` / `stream-json`（仅 `--print`） | SDK 固定 `stream-json` (`:408`) |
| `--permission-mode <mode>` | `acceptEdits`/`auto`/`bypassPermissions`/`manual`/`dontAsk`/`plan` | `permission_mode` → `:286` |
| `--model <model>` | 别名 `fable`/`opus`/`sonnet` 或全名 `claude-fable-5` | `model` → `:272` |
| `--system-prompt <prompt>` | 系统 prompt | `system_prompt` (str) → `:230` |
| `--append-system-prompt <prompt>` | 追加到默认 prompt | preset+append → `:237` |
| `--add-dir <dirs...>` | 额外允许目录 | `add_dirs` → `:305` |
| `--settings <file-or-json>` | 额外 settings | `settings` → `:300` |
| `--setting-sources <sources>` | `user,project,local` | `setting_sources` → `:353` |
| `--mcp-config <configs...>` | MCP server JSON 文件或字符串 | `mcp_servers` → `:326-332` |
| `--strict-mcp-config` | 只用 `--mcp-config` 的 server | `strict_mcp_config` → `:341` |
| `--max-budget-usd <amount>` | USD 上限（仅 `--print`） | `max_budget_usd` → `:263` |
| `--json-schema <schema>` | 结构化输出 schema | `output_format` → `:404` |
| `--betas <betas...>` | beta headers（API key 用户） | `betas` → `:278` |
| `--effort <level>` | `low`/`medium`/`high`/`xhigh`/`max` | `effort` → `:393` |
| `--resume <id>` / `-r` | 续接 session | `resume` → `:292` |
| `--session-id <uuid>` | 指定 session UUID | `session_id` → `:295` |
| `--fork-session` | resume 时分叉 | `fork_session` → `:344` |
| `--continue` / `-c` | 续接最近会话 | `continue_conversation` → `:289` |
| `--include-partial-messages` | partial 消息（仅 print + stream-json） | `include_partial_messages` → `:335` |
| `--include-hook-events` | hook 事件入流（仅 stream-json） | `include_hook_events` → `:338` |
| `--no-session-persistence` | 不持久化（仅 print） | 通过 `extra_args` 注入 |
| `--plugin-dir <path>` | 本地插件 | `plugins` → `:359` |
| `--worktree [name]` / `-w` | 创建 git worktree | 星射线用 Python 层自管 worktree，不用此 flag |
| `--verbose` | verbose 模式 | SDK 固定带 (`:225`) |
| `--dangerously-skip-permissions` | 绕过所有权限（沙箱用） | Full mode 备选，非默认 |

### A.2 `claude --help` 中**不存在**的 flag（SDK 仍在传 — Step0 已实测全部接受）

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
> ⚠️ **关键**：这四个 flag 必须配合 `--verbose` 才能跑通 —— 不带 `--verbose` 时 `-p --output-format stream-json` 直接 exit=1（与 flag 本身无关，是 stream-json 对 `--verbose` 的强制依赖，见 A.4）。
> ✅ `--cwd` 确认不存在 —— Full mode `_spawn_cli` 已改用 `create_subprocess_exec(cwd=worktree)`。

### A.3 `PermissionMode` SDK vs CLI 差异

| | SDK `PermissionMode` (`types.py:24`) | CLI `--permission-mode` choices (`claude --help`) |
|---|---|---|
| 共有 | `acceptEdits` / `plan` / `bypassPermissions` / `dontAsk` / `auto` | 同 |
| 差异 | `default` | `manual` |

- SDK 的 `default` = 不传 `--permission-mode` flag（走 CLI 默认行为）。
- CLI 的 `manual` 在 SDK 中无对应（可能 SDK 用 `default` 涵盖，或 `manual` 是 CLI 交互模式专用）。
- Full mode `_spawn_cli` 若需指定，用 `--permission-mode acceptEdits`（子进程在隔离 worktree，安全）。

### A.4 Full mode `_spawn_cli` 正确命令模板（Step0 实测后）

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
1. **`cwd=worktree`**（不是 `--cwd` flag）—— CLI 无 `--cwd`；Step0 实测 CLI 读 cwd/.claude 配置，worktree 隔离下子进程读各自项目配置。
2. **`--tools`（非 `--allowedTools`）** —— Step0 实测两者语义不同：`--tools` 是**可见工具白名单**（白名单外工具对模型不可见，模型不调用）；`--allowedTools` 是**自动允许列表**（工具仍可见，调用被拒记入 `permission_denials`，多耗 1 turn + ~2× cost）。L0/L1 只读约束必须用 `--tools`。
3. **`--permission-mode acceptEdits`** —— worktree 隔离下安全；`-p` 模式权限拒绝正常 exit=0 记入 `permission_denials`，不卡死（Step0 实测）。
4. **`--verbose` 必需** —— Step0 实测：`-p --output-format stream-json` 不带 `--verbose` 则 exit=1（`requires --verbose`）；带后 stderr 空、不污染 stdout。
5. **prompt 作为位置参数** —— `-p` 模式下 prompt 作位置参数；`create_subprocess_exec` 不走 shell，参数转义安全。

### A.5 stream-json 输出结构（Step0 已抓取 2026-07-05）

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
