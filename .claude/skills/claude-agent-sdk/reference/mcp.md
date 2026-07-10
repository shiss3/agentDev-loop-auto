# MCP（Model Context Protocol）

> 源码: `__init__.py:307` (`create_sdk_mcp_server`)，`__init__.py:166` (`tool`)，`__init__.py:155` (`SdkMcpTool`)，`types.py:602-637` (配置类型)

## 创建进程内 MCP server

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

## 用法

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

## 四种 `McpServerConfig`（`types.py:602-637`）

| 类型 | 字段 | 说明 |
|------|------|------|
| `McpStdioServerConfig` | `command`, `args`, `env` | 子进程 stdio |
| `McpSSEServerConfig` | `url`, `headers` | SSE |
| `McpHttpServerConfig` | `url`, `headers` | HTTP |
| `McpSdkServerConfig` | `name`, `instance` | 进程内（`create_sdk_mcp_server` 返回） |

`ClaudeSDKClient.get_mcp_status()` -> `McpStatusResponse`（`client.py:473`）。

## 星射线落地

GCH（全局上下文中枢）规划为 MCP server（`core/gch/server.py`）。但 **L0 Router 当前不挂 GCH**--L0 只做路由判定 + Fast Lane 执行。GCH 在 L1/L2 启用时通过 `mcp_servers={"gch": ...}` 挂载，L0/L1 读写全局记忆，L2 不挂载。当前 Phase 2 L0 阶段不启用，Phase 2 P0 后期或 Phase 3+ 评估。
