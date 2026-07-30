# 结构化输出、上下文用量与消息类型

## 结构化输出: `output_format` -> `ResultMessage.structured_output`（★ 路由判定）

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

## 上下文用量: `get_context_usage()`

> 源码: `client.py:506`，`ContextUsageResponse` (`types.py:759`)

```python
usage = await client.get_context_usage()
# usage.percentage: float (0-100)         ← ContextGuard 驱动 kill/respawn
# usage.isAutoCompactEnabled: bool
# usage.totalTokens / usage.maxTokens / usage.rawMaxTokens
# usage.categories: list[ContextUsageCategory]  (system prompt / tools / messages ...)
```

**星射线落地**: `ContextGuard`（Phase 2 P2）用 `percentage` 驱动压缩/kill/respawn 决策。CLAUDE.md 速查表里标注此项「开箱即用」。

## 消息类型

> 源码: `types.py:920-1167`

| 类型 | 行 | 用途 |
|------|----|------|
| `UserMessage` | 1014 | 用户消息 |
| `AssistantMessage` | 1024 | 模型消息，`.content: list[ContentBlock]` |
| `TextBlock` | 920 | 文本块 (`text`) |
| `ToolUseBlock` | 935 | 工具调用块 (`id`, `name`, `input`) |
| `ToolResultBlock` | 945 | 工具结果块 |
| `ThinkingBlock` | - | 思考块 |
| `SystemMessage` | 1040 | 系统消息 |
| `ResultMessage` | 1144 | 结算消息（见上） |
| `StreamEvent` | 1170 | partial 流事件 (`event` 是原始 Anthropic API 事件) |
| `RateLimitEvent` | 1214 | 限流状态变化（`allowed_warning`/`rejected`） |

`Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | ...`（联合类型）。
