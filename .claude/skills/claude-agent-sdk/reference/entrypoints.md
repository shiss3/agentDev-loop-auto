# 执行入口：query() 与 ClaudeSDKClient

SDK 提供两个入口，星射线两个都用。

## `query()` - 一次性 / 无状态 / 单向

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

## `ClaudeSDKClient` - 有状态 / 交互式 / 双向

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
| `get_mcp_status()` | 473 | -> `McpStatusResponse` |
| `get_context_usage()` | 506 | -> `ContextUsageResponse`（**ContextGuard 用**） |
| `get_server_info()` | 542 | -> server 信息 dict |
| `receive_response()` | 567 | `AsyncIterator[Message]`，收单轮响应 |
| `disconnect()` | 608 | 关闭连接 |

- **星射线落地**: **L0 Fast Lane 常驻会话**用 `ClaudeSDKClient`（已在 `chat/session.py` 的 `ChatSession` 里封装）。Fast Lane 走 `client.query(text)` 追加对话、`receive_messages()` 流式渲染、`interrupt()` 实现 `cancel()`。
