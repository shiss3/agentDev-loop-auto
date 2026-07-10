# Phase 2 落地映射总表 + 关键核对结论

## 星射线 Phase 2 落地映射总表

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
| 原生 subagent | `AgentDefinition` + `agents` | L1->L2 委派备选 | ⬜ 当前用 CLI 子进程替代 |
| MCP | `create_sdk_mcp_server` / `mcp_servers` | GCH 总线 | ⬜ L1/L2 启用时 |
| 插件 | `SdkPluginConfig` / `plugins` | - | ⬜ 不用 |
| Skills | `ClaudeAgentOptions.skills` / `AgentDefinition.skills` | Skill 知识库 | ⬜ Phase 4 |

## 关键核对结论（修正项）

1. **skills 是 SDK 原生 Python API 能力**（`ClaudeAgentOptions.skills` + `AgentDefinition.skills`），不是只能通过 CLI 使用的概念。早期「skills 是 CLI 专属」的判断**作废**。
2. **`mcp` 包是 SDK 硬依赖**（`__init__.py:19` 顶部导入），不是可选依赖。项目须确保 `mcp` 已安装。
3. **`PermissionMode` 含 `auto`**（6 个值），`ClaudeAgentOptions.permission_mode` docstring 漏标，以 `types.py:24` Literal 为准。
4. **`tools` vs `allowed_tools` 区别**: `tools` 控制可见性（`[]` 彻底禁用），`allowed_tools` 只控制是否跳过权限询问。路由判定若要严格禁工具，`tools=[]` 比 `allowed_tools=[]` 更稳妥--架构师 agent.md §5 可复核此点。
5. **`create_sdk_mcp_server` / `tool` / `SdkMcpTool` 定义在 `__init__.py`**（非 `_sdk_mcp_server.py`，该文件不存在），`fork_session` / `tag_session` 在 `_internal/session_mutations.py`。
6. **Full mode 不经 SDK**: L0 Full mode 用 `claude -p` CLI 子进程，SDK 的 `can_use_tool` / `ResultMessage.total_cost_usd` / `output_format` 等对它**不生效**--权限走 CLI flag，cost 走 stdout 解析。SDK 能力只在 Fast Lane（常驻 SDK 会话）和路由判定（`query()` 顶层）路径生效。
