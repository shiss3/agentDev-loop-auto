# claude-agent-sdk 能力速查（v0.2.93）

> 本文件由实测 `claude-agent-sdk==0.2.93` 的真实 API 表面整理而成，作为 Harness Agent 项目（星射线三层编排）的 SDK 参考底座。
> 校验方式：`python -c "import claude_agent_sdk; ..."` 直接 introspect 类型与签名。
> 最后核对日期：2026-06-21。

---

## 0. 一句话定位

`claude-agent-sdk` 是 Claude Code CLI 的 Python 封装：它在子进程里拉起 Claude Code CLI，由 **CLI 内部跑完整的 ReAct 循环**（工具调用、上下文压缩、子 agent、会话持久化都在 CLI 侧），SDK 只负责**进程管理 + 双向消息流 + 配置注入**。

> ⚠️ **对 Harness 的关键含义**：真正的对话历史和上下文占用在 **CLI/session 内部**，不在你 LangGraph 的 `state["messages"]` 里。要管理上下文，必须操作 SDK 的 session（`resume` / `get_context_usage` / `PreCompact`），而不是去裁剪 LangGraph state。

---

## 1. 两种调用入口

### 1.1 `query()` — 一次性无状态调用

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(prompt="任务描述", options=ClaudeAgentOptions(...)):
    ...  # 消费消息流
```

签名：
```python
query(*, prompt: str | AsyncIterable[dict],
      options: ClaudeAgentOptions | None = None,
      transport: Transport | None = None) -> AsyncIterator[Message]
```

- `prompt` 可为字符串，也可为 `AsyncIterable[dict]`（流式/多轮输入）。
- **无跨调用记忆**：每次 `query()` 是独立 turn。要续接历史靠 `options.resume` / `continue_conversation`。
- Harness Phase 1 的 `BaseAgent` 用的就是这个。

### 1.2 `ClaudeSDKClient` — 有状态长连接

```python
from claude_agent_sdk import ClaudeSDKClient

client = ClaudeSDKClient(options=...)
await client.connect()
await client.query("第一轮", session_id="default")
async for msg in client.receive_response():
    ...
await client.query("第二轮")          # 同一连接续接
usage = await client.get_context_usage()
await client.interrupt()              # 中断当前任务
await client.disconnect()
```

`ClaudeSDKClient` 方法全集（实测）：

| 方法 | 作用 |
|------|------|
| `connect(prompt=None)` | 建立连接（可带首个 prompt） |
| `disconnect()` | 断开 |
| `query(prompt, session_id='default')` | 发送一轮输入（不返回，结果走 receive） |
| `receive_messages()` | 持续接收所有消息（不自动停） |
| `receive_response()` | 接收直到本轮 `ResultMessage` 结束 |
| `interrupt()` | 中断当前任务（对应 ESC 取消） |
| `stop_task(task_id)` | 停止指定后台任务 |
| `get_context_usage()` | **返回 `ContextUsageResponse`，真实上下文占用** |
| `get_mcp_status()` | MCP 服务器状态 |
| `get_server_info()` | 服务端信息 |
| `set_model(model=None)` | 运行时切模型 |
| `set_permission_mode(mode)` | 运行时切权限模式 |
| `rewind_files(user_message_id)` | **回滚文件到某条消息时的状态**（需 file checkpointing） |
| `reconnect_mcp_server(name)` / `toggle_mcp_server(name, enabled)` | MCP 动态管理 |

> **选型建议**：星射线 L2 子 Agent 若需"温启动续命"，用 `ClaudeSDKClient` 或 `query(resume=...)`；纯一次性执行用 `query()`。

---

## 2. `ClaudeAgentOptions` 全字段（实测 44 个）

按功能分组。**粗体**为对 Harness 编排最关键的字段。

### 2.1 模型与推理
| 字段 | 类型 | 说明 |
|------|------|------|
| **`model`** | `str \| None` | 主模型 ID（如 `claude-sonnet-4-6`） |
| `fallback_model` | `str \| None` | 主模型失败时回退 |
| **`effort`** | `'low'\|'medium'\|'high'\|'xhigh'\|'max'` | 推理力度档位 |
| `thinking` | `ThinkingConfigAdaptive\|Enabled\|Disabled\|None` | thinking 配置 |
| `max_thinking_tokens` | `int \| None` | thinking token 上限 |
| `betas` | `list['context-1m-2025-08-07']` | **开启 1M 上下文窗口** |

### 2.2 工具与权限
| 字段 | 类型 | 说明 |
|------|------|------|
| `tools` | `list[str] \| ToolsPreset \| None` | 工具集（或 preset） |
| **`allowed_tools`** | `list[str]` | 白名单（如 `["Read","Write","Edit","Bash"]`） |
| `disallowed_tools` | `list[str]` | 黑名单 |
| **`permission_mode`** | `'default'\|'acceptEdits'\|'plan'\|'bypassPermissions'\|'dontAsk'\|'auto'` | 权限模式 |
| `can_use_tool` | `Callable[..., Awaitable[PermissionResult]]` | **自定义工具准入回调**（编程式拦截） |
| `permission_prompt_tool_name` | `str \| None` | 权限确认走哪个工具 |

### 2.3 会话与续接（温启动核心）
| 字段 | 类型 | 说明 |
|------|------|------|
| **`resume`** | `str \| None` | **恢复指定 session_id 的历史**（真温启动） |
| **`continue_conversation`** | `bool` | 续接最近一次会话 |
| `session_id` | `str \| None` | 指定会话 ID |
| **`fork_session`** | `bool` | 从恢复点分叉新会话（不污染原会话） |
| `session_store` | `SessionStore \| None` | 自定义会话存储后端 |
| `session_store_flush` | `'batched'\|'eager'` | 存储刷盘策略 |

### 2.4 上下文与持久化
| 字段 | 类型 | 说明 |
|------|------|------|
| **`enable_file_checkpointing`** | `bool` | **开启文件检查点**（配合 `rewind_files` 回滚） |
| `add_dirs` | `list[str\|Path]` | 额外可访问目录 |
| **`cwd`** | `str\|Path\|None` | 工作目录 |

### 2.5 成本与轮次控制
| 字段 | 类型 | 说明 |
|------|------|------|
| `max_turns` | `int \| None` | 最大 ReAct 轮次 |
| **`max_budget_usd`** | `float \| None` | **美元成本硬上限** |
| **`task_budget`** | `TaskBudget \| None` | 任务预算（`{total: int}`） |
| `max_buffer_size` | `int \| None` | 消息缓冲上限 |
| `load_timeout_ms` | `int` | 加载超时 |

### 2.6 扩展能力
| 字段 | 类型 | 说明 |
|------|------|------|
| **`agents`** | `dict[str, AgentDefinition] \| None` | **原生子 agent 定义**（见 §3） |
| **`hooks`** | `dict[HookEvent, list[HookMatcher]]` | **生命周期钩子**（见 §5） |
| `mcp_servers` | `dict[...] \| str \| Path` | MCP 服务器（见 §4） |
| `strict_mcp_config` | `bool` | 严格 MCP 校验 |
| `skills` | `list[str] \| 'all' \| None` | 启用的 skills |
| `plugins` | `list[SdkPluginConfig]` | 本地插件（`{type:'local', path}`） |
| **`sandbox`** | `SandboxSettings \| None` | **沙箱隔离**（见 §6，Phase 3 用） |
| `system_prompt` | `str \| SystemPromptPreset \| SystemPromptFile \| None` | 系统提示词（支持 preset / 文件） |
| `setting_sources` | `list['user'\|'project'\|'local'] \| None` | 配置来源 |
| `settings` | `str \| None` | settings 文件路径 |
| `output_format` | `dict \| None` | **结构化输出格式**（见 ResultMessage.structured_output） |

### 2.7 流式与调试
| 字段 | 类型 | 说明 |
|------|------|------|
| `include_partial_messages` | `bool` | 包含增量 token（细粒度流式） |
| `include_hook_events` | `bool` | 消息流里带 hook 事件 |
| `stderr` / `debug_stderr` | `Callable / Any` | stderr 回调 |
| `env` / `extra_args` / `user` / `cli_path` | — | 进程环境 / 透传 CLI 参数 / 用户标识 / CLI 路径 |

---

## 3. 原生子 Agent（`AgentDefinition`）

SDK 内置 subagent 机制 —— 主 agent 可委托子任务给命名子 agent，**无需自建执行循环**。

```python
from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

options = ClaudeAgentOptions(
    agents={
        "coder": AgentDefinition(
            description="编码执行专家",          # 必填：何时调用它
            prompt="你是纯执行编码 agent...",     # 必填：system prompt
            tools=["Read", "Write", "Edit", "Bash"],
            model="claude-sonnet-4-6",
            effort="high",
            maxTurns=20,
            permissionMode="acceptEdits",
            memory="project",                     # user|project|local 记忆作用域
            skills=["test-runner"],
            background=False,
        ),
    },
)
```

`AgentDefinition` 字段（实测）：
| 字段 | 类型 | 说明 |
|------|------|------|
| `description` | `str` | **必填**，主 agent 据此决定何时委派 |
| `prompt` | `str` | **必填**，子 agent 的 system prompt |
| `tools` | `list[str] \| None` | 工具白名单 |
| `disallowedTools` | `list[str] \| None` | 工具黑名单 |
| `model` | `str \| None` | 子 agent 模型 |
| `effort` | `'low'..'max' \| int \| None` | 推理力度 |
| `maxTurns` | `int \| None` | 子 agent 最大轮次 |
| `permissionMode` | 同上枚举 | 权限模式 |
| `memory` | `'user'\|'project'\|'local' \| None` | 记忆作用域 |
| `skills` | `list[str] \| None` | 启用的 skills |
| `mcpServers` | `list[str\|dict] \| None` | 子 agent 可用 MCP |
| `initialPrompt` | `str \| None` | 启动时的首条 prompt |
| `background` | `bool \| None` | 后台运行 |

> **对星射线的含义**：L1 主智能体可以把 L2 执行单元定义成 SDK 原生 `agents`，由 CLI 调度，省去 LangGraph 层自建 sub_agent 循环。子 agent 的消息/列表可用 `get_subagent_messages()` / `list_subagents()` 读取。

---

## 4. 自定义工具与 MCP

### 4.1 进程内 SDK 工具（`@tool` + `create_sdk_mcp_server`）

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("add", "两数相加", {"a": int, "b": int})
async def add(args) -> dict:
    return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}

server = create_sdk_mcp_server(name="calc", version="1.0.0", tools=[add])

options = ClaudeAgentOptions(mcp_servers={"calc": server})
```

- `@tool(name, description, input_schema)`：`input_schema` 可为 `{"a": int}` 简写或完整 JSON Schema。
- handler 是 `async (args) -> dict`，返回 MCP 内容块格式。
- `create_sdk_mcp_server` 返回 `McpSdkServerConfig`（`{type:'sdk', name, instance}`）—— **进程内运行，无需外部子进程**。

### 4.2 外部 MCP 服务器
`mcp_servers` 也接受 stdio / SSE / HTTP 配置：`McpStdioServerConfig` / `McpSSEServerConfig` / `McpHttpServerConfig`。运行时可用 `client.toggle_mcp_server()` / `reconnect_mcp_server()` / `get_mcp_status()` 管理。

---

## 5. Hooks（生命周期钩子）

10 个钩子事件（实测 `hooks` 字段的 Literal）：

| 事件 | 触发时机 | 典型用途 |
|------|---------|---------|
| **`PreToolUse`** | 工具调用前 | **代码审查拦截 / 准入控制**（Phase 3） |
| `PostToolUse` | 工具调用后 | 记录结果、二次校验 |
| `PostToolUseFailure` | 工具调用失败后 | 错误捕获（Phase 4 经验沉淀） |
| `UserPromptSubmit` | 用户提交 prompt 时 | 输入预处理/注入上下文 |
| `Stop` | 主 agent 停止时 | 收尾 |
| `SubagentStart` | **子 agent 启动** | 发射层级事件给 renderer |
| `SubagentStop` | **子 agent 结束** | 聚合子 agent 结果 |
| **`PreCompact`** | **CLI 即将压缩上下文前** | **注入核心经验记忆**（上下文管理核心） |
| `Notification` | 通知事件 | UI 提示 |
| `PermissionRequest` | 权限请求时 | 自定义授权 |

注册方式：
```python
from claude_agent_sdk import HookMatcher, HookContext, PreToolUseHookInput

async def review_hook(inp: PreToolUseHookInput, tool_use_id: str | None, ctx: HookContext):
    if inp["tool_name"] == "Bash" and "rm -rf" in str(inp["tool_input"]):
        return {"decision": "block", "reason": "危险命令"}   # 阻断
    return {}

options = ClaudeAgentOptions(
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[review_hook], timeout=10)]}
)
```

`HookMatcher`：`{matcher: str|None, hooks: list[callback], timeout: float|None}`。
钩子回调签名：`async (input, tool_use_id, HookContext) -> HookJSONOutput`。

关键 HookInput 字段（实测）：
- 通用：`session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`
- `PreToolUse`：`tool_name`, `tool_input`, `tool_use_id`, `agent_id`, `agent_type`
- `PostToolUse`：额外 `tool_response`
- `PreCompact`：`trigger`（`'manual'\|'auto'`）, `custom_instructions`
- `SubagentStart`：`agent_id`, `agent_type`
- `SubagentStop`：`agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`

---

## 6. 沙箱（`SandboxSettings`，Phase 3）

```python
from claude_agent_sdk import SandboxSettings, SandboxNetworkConfig

sandbox = SandboxSettings(
    enabled=True,
    autoAllowBashIfSandboxed=True,        # 沙箱内自动放行 Bash
    excludedCommands=["docker"],
    allowUnsandboxedCommands=False,
    network=SandboxNetworkConfig(
        allowedDomains=["api.anthropic.com"],
        deniedDomains=["*"],
        allowManagedDomainsOnly=True,
        allowLocalBinding=False,
    ),
    enableWeakerNestedSandbox=False,
)
```

`SandboxNetworkConfig` 还支持：`allowUnixSockets`, `allowAllUnixSockets`, `allowMachLookup`, `httpProxyPort`, `socksProxyPort`, `ignoreViolations`。

> Phase 3 用沙箱隔离 L2 子 agent 的执行，防止误操作逃逸。

---

## 7. 上下文用量监控（`ContextUsageResponse`）

`await client.get_context_usage()` 返回**真实**占用，是驱动压缩/KILL 决策的正确信号：

| 字段 | 说明 |
|------|------|
| `totalTokens` / `maxTokens` / `rawMaxTokens` | 当前 / 有效上限 / 原始上限 |
| **`percentage`** | **占用百分比**（直接拿来做阈值判断） |
| `model` | 当前模型 |
| `isAutoCompactEnabled` | 是否开启自动压缩 |
| `autoCompactThreshold` | 自动压缩阈值（NotRequired） |
| `categories` | 分类占用（`list[ContextUsageCategory]`） |
| `memoryFiles` / `mcpTools` / `agents` / `systemTools` | 各部分占用明细 |
| `messageBreakdown` / `apiUsage` | 消息级 / API 用量细分 |

> ⚠️ **不要**用手动 `count_tokens(state["messages"])` 估算占用 —— 那不是模型真实看到的上下文。用 `percentage` / `totalTokens`。

---

## 8. 会话持久化与管理（模块级函数）

SDK 提供一组**模块级**会话管理函数（不依赖 client 实例），底层按 `directory`（项目目录）分桶：

| 函数 | 签名要点 | 用途 |
|------|---------|------|
| `list_sessions(directory=None, limit=None, offset=0, include_worktrees=True)` | `-> list[SDKSessionInfo]` | 列出历史会话 |
| `get_session_info(session_id, directory=None)` | `-> SDKSessionInfo \| None` | 单会话元信息 |
| `get_session_messages(session_id, directory=None, limit, offset)` | `-> list[SessionMessage]` | 读会话消息 |
| `delete_session(session_id, directory=None)` | `-> None` | 删除会话 |
| `rename_session(session_id, title, directory=None)` | `-> None` | 重命名 |
| `tag_session(session_id, tag, directory=None)` | `-> None` | 打标签 |
| `fork_session(session_id, directory=None, up_to_message_id=None, title=None)` | `-> ForkSessionResult` | **分叉**（可指定截断点） |
| `list_subagents(session_id, directory=None)` | `-> list[str]` | 列出子 agent |
| `get_subagent_messages(session_id, agent_id, directory=None, ...)` | `-> list[SessionMessage]` | 读子 agent 消息 |
| `fold_session_summary(prev, key, entries)` | `-> SessionSummaryEntry` | **折叠摘要**（配合 PreCompact） |
| `project_key_for_directory(directory=None)` | `-> str` | 目录 → 项目 key |

还有一组 `*_via_store` / `*_from_store` 变体（如 `delete_session_via_store`, `list_sessions_from_store`, `import_session_to_store`），针对自定义 `SessionStore` 后端操作。

`SDKSessionInfo` 字段：`session_id`, `summary`, `last_modified`, `file_size`, `custom_title`, `first_prompt`, `git_branch`, `cwd`, `tag`, `created_at`。

> **对 Harness 的含义**：`harness chat` 的 `--continue` / `--resume` / 会话选择器，可直接建立在 `list_sessions` + `get_session_info` + `resume` 之上，无需自造会话存储。挂起池可用 `tag_session` 标记 suspended，`fork_session` 实现"从检查点分叉并行尝试"。

---

## 9. 消息与内容块类型

`query()` / `receive_*()` 产出的消息类型（实测字段）：

| 消息类型 | 关键字段 |
|---------|---------|
| `AssistantMessage` | `content`(blocks), `model`, `usage`, `stop_reason`, `session_id`, `parent_tool_use_id`, `error`, `message_id`, `uuid` |
| `UserMessage` | `content`, `tool_use_result`, `parent_tool_use_id`, `uuid` |
| `SystemMessage` | `subtype`, `data` |
| `ResultMessage` | `subtype`, `duration_ms`, `is_error`, `num_turns`, `total_cost_usd`, `usage`, `model_usage`, `result`, **`structured_output`**, `permission_denials`, `errors`, `api_error_status`, `session_id` |
| `StreamEvent` | `event`, `session_id`, `parent_tool_use_id`, `uuid`（`include_partial_messages=True` 时的增量） |
| `RateLimitEvent` | `rate_limit_info`, `session_id`, `uuid` |

内容块（`AssistantMessage.content` 里的元素）：
| Block | 字段 | 含义 |
|-------|------|------|
| `TextBlock` | `text` | 模型文本 |
| `ThinkingBlock` | `thinking`, `signature` | 思考过程 |
| `ToolUseBlock` | `id`, `name`, `input` | 工具调用意图 |
| `ToolResultBlock` | `tool_use_id`, `content`, `is_error` | 工具结果 |
| `ServerToolUseBlock` | `id`, `name`, `input` | 服务端工具调用 |
| `ServerToolResultBlock` | — | 服务端工具结果 |

> **流式渲染要点**（Harness 已踩过的坑）：模型输出在 `AssistantMessage.content` 的 block 里，**不在** LangChain 的 `on_chat_model_stream` 事件中（因为模型调用在 CLI 子进程内）。Harness 用 `adispatch_custom_event` 把 block 转成自定义事件给 renderer。`ResultMessage.model_usage` 是真实用量来源。

### 9.1 结构化输出
设 `options.output_format`，模型结果会填进 `ResultMessage.structured_output`。可用于 Architect 一次性输出"任务拆解 + 依赖 + 提示词" JSON，省去解析。

---

## 10. 异常类型

| 异常 | 含义 |
|------|------|
| `ClaudeSDKError` | 基类 |
| `CLINotFoundError` | 找不到 Claude Code CLI 可执行文件 |
| `CLIConnectionError` | 连接 CLI 失败 |
| `CLIJSONDecodeError` | CLI 输出 JSON 解析失败 |
| `ProcessError` | 子进程错误 |
| `MirrorErrorMessage` | 镜像错误消息 |

> Harness `BaseAgent` 已用 `try/except` 兜住，把异常转成 `agent_error` 自定义事件，避免静默崩溃。建议针对 `CLINotFoundError` 给出"未安装 CLI"的明确提示。

---

## 11. 与星射线三层架构的映射速查

| 星射线需求 | SDK 能力 | 关键 API |
|-----------|---------|---------|
| L2 执行单元 | 原生子 agent / query | `agents` / `query()` |
| L2 温启动续命 | session 续接 | `resume` / `session_id` / `ClaudeSDKClient` |
| 并行尝试不污染 | session 分叉 | `fork_session` |
| 上下文压缩决策 | 真实用量 | `get_context_usage().percentage` |
| 注入核心经验 | 压缩钩子 | `PreCompact` hook + `fold_session_summary` |
| 成本硬上限 | 预算 | `max_budget_usd` / `task_budget` |
| 代码可回滚（验收/打回） | 文件检查点 | `enable_file_checkpointing` + `rewind_files()` |
| 代码审查拦截（Phase 3） | 工具钩子 | `PreToolUse` hook / `can_use_tool` |
| 沙箱隔离（Phase 3） | 沙箱 | `sandbox=SandboxSettings(...)` |
| 会话恢复/选择器 | 会话管理 | `list_sessions` / `get_session_info` / `resume` |
| 挂起池存储 | 自定义存储 | `session_store` / `tag_session` |
| 自定义领域工具 | 进程内 MCP | `@tool` / `create_sdk_mcp_server` |
| 子 agent 生命周期事件 | 子 agent 钩子 | `SubagentStart` / `SubagentStop` |
| 1M 长上下文 | beta | `betas=["context-1m-2025-08-07"]` |

---

## 12. 反模式（Harness 项目踩坑记录）

1. **❌ 压缩 LangGraph `state["messages"]` 来省 token** —— 它不回喂模型，无效。✅ 用 `get_context_usage()` + `PreCompact`。
2. **❌ 靠 `query()` 实现"记得上次代码"** —— query 无记忆。✅ 用 `resume` / `ClaudeSDKClient`。
3. **❌ 监听 `on_chat_model_stream` 做流式** —— 模型在 CLI 子进程内，收不到。✅ 用 `adispatch_custom_event` 转 block。
4. **❌ 自建子 agent 执行循环** —— 重复造轮子。✅ 优先 `agents` 原生子 agent。
5. **❌ 用 LangGraph Checkpointer 当 KV 存任意对象** —— 它按 thread 存整图快照。✅ 会话用 `session_store` / 模块级 session 函数。
6. **❌ 手动 `count_tokens` 估算窗口占用** —— 不准。✅ `ContextUsageResponse.percentage`。

---

> 版本锁定建议：`claude-agent-sdk==0.2.93`。该 SDK API 表面较大且仍在演进，升级前用 `python -c "import claude_agent_sdk, dataclasses; ..."` 重新核对字段，并把 SDK 能力封装在 Harness 的适配层后面，隔离版本变动。
