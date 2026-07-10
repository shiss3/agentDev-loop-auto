---
name: Codex-agent-sdk
description: 速查 Codex-agent-sdk (v0.2.93) Python API。当基于 Codex-agent-sdk 开发、需要确认 API 签名/字段/用法时使用。入口只给能力路由表，按 API 类型 Read 对应 reference/ 子文档，避免一次性加载全文。
---

# Codex-agent-sdk API 速查（入口）

> **SDK 版本**: v0.2.93
> **核对来源**: `.venv/Lib/site-packages/claude_agent_sdk/` 源码逐字段核对（非记忆）
> **核对日期**: 2026-07-05
> **范围**: 项目中可能会使用到的 Codex-agent-sdk 的 api。
> **硬依赖**: `mcp` 包。`__init__.py:19` 顶部 `from mcp.types import ToolAnnotations` 是模块级硬导入——未安装 `mcp` 包时整个 SDK 无法 import。`create_sdk_mcp_server()` 内部还 `from mcp.server import Server`（函数级导入）。

## 怎么用这个 skill

本入口只列能力路由表。按你要查的 API 类型，**Read 对应的 `reference/` 子文档**，不要一次全读。

## 能力路由表

| 想查 | 读这个文件 |
|------|-----------|
| 两个执行入口 `query()` vs `ClaudeSDKClient`（最关键的选择） | [reference/entrypoints.md](reference/entrypoints.md) |
| `ClaudeAgentOptions` 全 37 字段（执行 / 工具权限 / 系统提示 / 输出 / 预算 / MCP / hooks…） | [reference/options.md](reference/options.md) |
| `PermissionMode` 取值 + 动态权限 `can_use_tool` / `PermissionResult` | [reference/permissions.md](reference/permissions.md) |
| 结构化输出 `output_format` / `ResultMessage` / 上下文用量 / 消息类型 | [reference/output-and-messages.md](reference/output-and-messages.md) |
| 原生 subagent（`AgentDefinition` + `agents`） | [reference/subagents.md](reference/subagents.md) |
| MCP（`create_sdk_mcp_server` / `tool` / `McpServerConfig`） | [reference/mcp.md](reference/mcp.md) |
| 插件（`SdkPluginConfig` / `plugins`） | [reference/plugins.md](reference/plugins.md) |
| SDK 原生 skills（`ClaudeAgentOptions.skills`） | [reference/skills.md](reference/skills.md) |
| 会话持久化（`SessionStore` / `fork_session` / `tag_session`） | [reference/sessions.md](reference/sessions.md) |
| Hooks（`HookEvent` / `HookMatcher` / 派发） | [reference/hooks.md](reference/hooks.md) |
| Full mode CLI headless 标志（`Codex -p` 命令构造 / stream-json 解析） | [reference/cli-headless.md](reference/cli-headless.md) |
| Phase 2 落地映射总表 + 关键核对结论 | [reference/summary.md](reference/summary.md) |

## 检索建议

- 不确定走哪个入口？先看 [entrypoints.md](reference/entrypoints.md) 确定是 `query()` 还是 `ClaudeSDKClient` 路径，再按需展开。
- 找字段定义？直奔 [options.md](reference/options.md)。
- Full mode（CLI 子进程）路径的权限 / cost / 输出与 SDK 进程内不同，见 [cli-headless.md](reference/cli-headless.md) 与 [summary.md](reference/summary.md) 的核对结论。
