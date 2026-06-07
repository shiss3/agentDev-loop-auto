# Claude Agent SDK — 方案更新（已确认 Python SDK 可用）

## 🎉 重大发现

Anthropic 官方已将 `claude-code-sdk` 正式升级并重命名为 **`claude-agent-sdk`**！

| 项目 | 详情 |
|------|------|
| 包名 | `claude-agent-sdk` |
| 版本 | **0.2.93**（2026-06-06 发布，今天！）|
| 安装 | `pip install claude-agent-sdk` |
| Python | ≥ 3.10 |
| License | MIT |
| GitHub | https://github.com/anthropics/claude-agent-sdk-python（⭐7.2k） |
| 官方文档 | https://docs.anthropic.com/en/docs/claude-code/sdk |
| Claude CLI | **SDK 内置打包，无需单独安装！** |

---

## ❌ 之前的方案：Node.js 桥接（已废弃）

之前推荐 Node.js 桥接服务是因为以为只有 Node.js SDK。
现在 Python SDK 原生支持，**不再需要任何桥接层**。

---

## ✅ 新推荐方案：Python 直接调用 claude-agent-sdk

### 核心 API

#### 1. 简单查询：`query()`

```python
import anyio
from claude_agent_sdk import query

async def main():
    async for message in query(prompt="What is 2 + 2?"):
        print(message)

anyio.run(main)
```

#### 2. 交互式客户端：`ClaudeSDKClient`（推荐用于 Harness）

`ClaudeSDKClient` 支持**双向、交互式对话**，比 `query()` 更强大：

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

options = ClaudeAgentOptions(
    system_prompt="你是前端开发专家",
    cwd="/path/to/project",
    allowed_tools=["Read", "Write", "Bash"],
    max_turns=10,
)

async with ClaudeSDKClient(options=options) as client:
    await client.query("创建一个 React 组件")
    async for msg in client.receive_response():
        print(msg)
```

#### 3. 自定义工具（In-Process MCP Server）

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions, ClaudeSDKClient

@tool("run_tests", "在 Docker 中运行测试", {"test_path": str})
async def run_tests(args):
    # 你的 Docker 沙箱测试逻辑
    result = await run_in_docker(args["test_path"])
    return {
        "content": [{"type": "text", "text": f"测试结果: {result}"}]
    }

server = create_sdk_mcp_server(
    name="harness-tools",
    version="1.0.0",
    tools=[run_tests]
)

options = ClaudeAgentOptions(
    mcp_servers={"harness": server},
    allowed_tools=["mcp__harness__run_tests"]
)
```

#### 4. Hooks（拦截 Agent 行为）

```python
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

async def review_before_write(input_data, tool_use_id, context):
    """在每次文件写入前进行审查"""
    tool_name = input_data["tool_name"]
    if tool_name != "Write":
        return {}

    file_path = input_data["tool_input"].get("file_path", "")
    content = input_data["tool_input"].get("content", "")

    # 在 Docker 沙箱中验证
    is_valid = await validate_in_sandbox(file_path, content)

    if not is_valid:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "代码未通过沙箱验证",
            }
        }
    return {}

options = ClaudeAgentOptions(
    hooks={
        "PreToolUse": [
            HookMatcher(matcher="Write", hooks=[review_before_write]),
        ],
    }
)
```

---

## 对 Harness Agent 项目的影响

### 架构大幅简化

```
之前：Python LangGraph → HTTP → Node.js Bridge → Claude Code SDK
现在：Python LangGraph → claude-agent-sdk（直接调用）
```

### 具体变化

| 模块 | 之前 | 现在 |
|------|------|------|
| SDK 调用 | 需要 Node.js 桥接服务 | **Python 直接 `pip install`** |
| 会话保持 | 需要桥接层管理 session | **SDK 原生 Session 支持** |
| 流式输出 | 需要 SSE 桥接 | **SDK 原生 AsyncIterator** |
| 自定义工具 | 需要外部 MCP 进程 | **In-Process SDK MCP Server** |
| 代码审查 | 需要自己实现 | **Hooks 系统（PreToolUse）** |
| 技术栈 | Python + Node.js | **纯 Python** |

### 新的推荐项目结构

```
harness-agent/
├── core/
│   ├── orchestrator.py      # LangGraph 编排
│   ├── context.py           # 共享上下文管理
│   └── agents/              # Agent 定义
│       ├── base_agent.py    # 基于 ClaudeSDKClient 的 Agent 基类
│       ├── frontend_agent.py
│       ├── api_agent.py
│       ├── backend_agent.py
│       └── skills_agent.py
├── tools/                   # 自定义 MCP 工具
│   ├── sandbox_tool.py      # Docker 沙箱验证
│   ├── test_runner.py       # 测试运行器
│   └── skills_writer.py     # Skills 写入工具
├── hooks/                   # Hooks（行为拦截）
│   ├── code_review_hook.py  # 代码写入前审查
│   └── error_capture_hook.py # 错误捕获 → 触发经验沉淀
├── skills/                  # 沉淀的经验
│   └── *.md
├── server/                  # FastAPI 后端
│   ├── main.py
│   ├── routes/
│   └── websocket.py
├── web/                     # React + TypeScript 前端（Web 版仪表盘）
├── app/                     # Flutter 前端（跨端）
├── cli.py                   # CLI 入口
├── pyproject.toml
└── Dockerfile
```

### 关键优势

1. **Hooks 天然解决"不 review 代码"的问题**
   - `PreToolUse` Hook 在写文件前自动审查
   - 不通过审查直接 deny，强制重写

2. **自定义工具天然解决"Docker 沙箱验证"**
   - `@tool` 装饰器定义 `run_in_sandbox` 工具
   - Claude 自动在沙箱中验证代码

3. **Session 天然解决"上下文共享"**
   - SDK 原生 Session 支持
   - 多个 Agent 可以通过 LangGraph State 共享上下文

4. **纯 Python 栈**
   - LangGraph + claude-agent-sdk + FastAPI 全部 Python
   - 无需跨语言桥接，部署和维护更简单
