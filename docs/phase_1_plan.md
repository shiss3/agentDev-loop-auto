# Phase 1: 最小可用核心（MVP）— 修正版 v3

> **版本**: v3.0 | **日期**: 2026-06-07 | **依赖管理**: uv
> **v2 修正**: 流式输出职责倒置 / Router 过早抽象 / 缺少 dotenv
> **v3 修正**: ToolUse 静音 / LangGraph-SDK 脑裂 / Prompt Caching 一厢情愿

---

## 🎯 阶段目标

**一句话**: 用最短路径验证 `claude-agent-sdk` → `LangGraph` → `CLI` 这条数据管道的畅通。

**不做的事**（明确排除）:
- ❌ 多 Agent 路由（Phase 2）
- ❌ Docker 沙箱验证（Phase 3）
- ❌ Hooks 代码审查（Phase 3）
- ❌ Skills 经验沉淀（Phase 4）
- ❌ Web/Flutter 前端（Phase 5）

---

## 🏗️ 架构设计

### 数据流

```
用户终端 (CLI)
    │
    │ harness run "重构 user.py"
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│  cli.py  — 流式渲染层                                      │
│                                                            │
│  async for event in graph.astream_events(...):             │
│      match event.name:                                     │
│          "agent_text"  → print(text)          # 思考过程    │
│          "agent_tool"  → print(🛠️ 工具名...)  # 工具调用    │
│          "agent_result"→ print(✅/❌ 结果)     # 工具结果    │
│          "agent_error" → print(💥 错误)        # 异常       │
│                                                            │
└────────────────────────┬──────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────┐
│  LangGraph StateGraph                                      │
│                                                            │
│  START ──→ default_agent ──→ END                           │
│                                                            │
│  ⚠️ State.messages 仅作最终结果归档                          │
│  ⚠️ Agent 内部 ReAct 循环由 SDK 内部状态机闭环               │
│                                                            │
└────────────────────────┬──────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────┐
│  BaseAgent (纯函数，无 IO)                                  │
│                                                            │
│  Input:  HarnessState                                      │
│  处理:                                                      │
│    1. 调用 query(prompt=task, options=...)                  │
│    2. 遍历 SDK 返回的所有 Message                            │
│    3. 对每种 Block 类型发射不同的自定义事件:                    │
│       - TextBlock      → adispatch("agent_text",  ...)     │
│       - ToolUseBlock   → adispatch("agent_tool",  ...)     │
│       - ToolResultBlock→ adispatch("agent_result", ...)    │
│    4. 组装完整结果 → 返回 State update                       │
│  Output: {"messages": [AIMessage(...)]}                    │
│                                                            │
│  ❌ 绝不 print / 绝不持有流 / 绝不阻塞                      │
│                                                            │
└────────────────────────┬──────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────┐
│  claude-agent-sdk                                          │
│                                                            │
│  query(prompt, options) → AsyncIterator[Message]           │
│                                                            │
│  返回的 Message 类型:                                       │
│  ┌─────────────────┬──────────────────────────────────┐    │
│  │ AssistantMessage │ 包含 TextBlock / ToolUseBlock    │    │
│  │ ResultMessage    │ 包含 ToolResultBlock             │    │
│  │ SystemMessage    │ 系统消息                         │    │
│  └─────────────────┴──────────────────────────────────┘    │
│                                                            │
│  ⚠️ SDK 内部自带 ReAct 循环 + 工具执行 + 上下文管理          │
│  ⚠️ Prompt Caching 需验证 SDK 是否原生支持                   │
│                                                            │
└───────────────────────────────────────────────────────────┘
```

### 五个关键设计原则

| # | 原则 | 错误做法 | 正确做法 |
|---|------|---------|---------|
| 1 | **流式输出** | BaseAgent 内部 `print(chunk)` | BaseAgent 只发射事件；CLI 层拦截渲染 |
| 2 | **状态图** | START → Router → Agent → END | START → Agent → END（奥卡姆剃刀） |
| 3 | **工具可见** | 只捕获 TextBlock，工具调用静音 | **捕获所有 Block 类型**，分类发射事件 |
| 4 | **状态管理** | 假装 LangGraph 和 SDK 共享上下文 | **明确脑裂**：SDK 内部闭环，State 仅归档 |
| 5 | **Prompt Caching** | 定义常量就以为能省钱 | **必须验证** API 层是否生效，写进冒烟测试 |

---

## 📅 任务分解

### Step 1: 项目骨架与环境搭建（~2h）

#### 1.1 使用 uv 初始化项目

```bash
# 安装 uv（如果尚未安装）
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

cd E:\front\html\harness-claude-code
uv init --name harness-agent --python ">=3.10"
```

#### 1.2 创建目录结构

```
harness-agent/
├── src/
│   └── harness_agent/
│       ├── __init__.py
│       ├── cli.py              # CLI 入口（流式渲染层）
│       ├── config.py           # 配置定义
│       └── core/
│           ├── __init__.py
│           ├── orchestrator.py # LangGraph 编排（极简直线图）
│           ├── state.py        # HarnessState 类型定义
│           └── agents/
│               ├── __init__.py
│               └── base_agent.py  # 纯函数 Agent（分类事件发射）
├── tests/
│   ├── __init__.py
│   ├── test_smoke.py           # 冒烟测试：SDK 连通 + Caching 验证
│   └── test_orchestrator.py    # 编排测试
├── docs/
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

#### 1.3 编写 pyproject.toml

```toml
[project]
name = "harness-agent"
version = "0.1.0"
description = "驾驭工程实践 — 基于 Claude Agent SDK 的智能开发编排系统"
requires-python = ">=3.10"

dependencies = [
    "claude-agent-sdk>=0.2.90",
    "langgraph>=0.2.0",
    "langchain-core>=0.3.0",
    "click>=8.1.0",
    "anyio>=4.0.0",
    "python-dotenv>=1.0.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.5.0",
]

[project.scripts]
harness = "harness_agent.cli:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

#### 1.4 环境变量

```bash
# .env.example
ANTHROPIC_API_KEY=sk-ant-xxxxx
HARNESS_PROJECT_DIR=.
HARNESS_LOG_LEVEL=INFO
```

#### 1.5 冒烟测试（含 Caching 验证）

```python
# tests/test_smoke.py
"""冒烟测试：SDK 连通性 + Prompt Caching 验证"""
import pytest
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

# ─── 测试 1：基础导入 ───
@pytest.mark.asyncio
async def test_sdk_import():
    """验证 SDK 核心类型可正确导入"""
    options = ClaudeAgentOptions(max_turns=1)
    assert options is not None

# ─── 测试 2：基础调用 ───
@pytest.mark.asyncio
async def test_sdk_simple_query():
    """验证 SDK 可以完成最简单的一次请求"""
    messages = []
    async for msg in query(prompt="回复'ok'", options=ClaudeAgentOptions(max_turns=1)):
        messages.append(msg)
    assert len(messages) > 0

# ─── 测试 3：Message 类型完整性 ───
@pytest.mark.asyncio
async def test_sdk_message_types():
    """验证 SDK 返回的 Message 包含哪些 Block 类型
    
    这个测试的目的是探测 SDK 的实际返回结构，
    为 BaseAgent 的事件分类提供依据。
    """
    from claude_agent_sdk import (
        AssistantMessage, ResultMessage, SystemMessage,
        TextBlock, ToolUseBlock, ToolResultBlock,
    )

    block_types_seen = set()
    message_types_seen = set()

    async for msg in query(
        prompt="读取当前目录的文件列表",
        options=ClaudeAgentOptions(
            max_turns=2,
            allowed_tools=["Bash"],
        ),
    ):
        message_types_seen.add(type(msg).__name__)
        if hasattr(msg, "content"):
            for block in msg.content:
                block_types_seen.add(type(block).__name__)

    print(f"\n[探测结果] Message 类型: {message_types_seen}")
    print(f"[探测结果] Block 类型:   {block_types_seen}")

    # 至少应该看到 AssistantMessage
    assert "AssistantMessage" in message_types_seen

# ─── 测试 4：Prompt Caching 探测 ───
@pytest.mark.asyncio
async def test_prompt_caching_support():
    """探测 claude-agent-sdk 是否支持 Prompt Caching
    
    方法：用相同的 system_prompt 连续发送两次请求，
    观察 SDK 是否暴露了 cache 相关的 usage 信息。
    
    ⚠️ 这是一个探测性测试，不是断言性测试。
    结果需要人工判读，决定后续 Caching 策略。
    """
    system = "你是一个测试助手。" * 100  # 故意加长以触发 Caching 阈值

    options = ClaudeAgentOptions(
        system_prompt=system,
        max_turns=1,
    )

    # 第一次请求：预期 cache_creation_input_tokens > 0
    msgs_1 = []
    async for msg in query(prompt="说1", options=options):
        msgs_1.append(msg)
        # 检查 msg 是否有 usage 属性
        if hasattr(msg, "usage"):
            print(f"\n[Caching 探测 - 请求1] usage: {msg.usage}")
        if hasattr(msg, "model_dump"):
            dump = msg.model_dump()
            if "usage" in dump:
                print(f"\n[Caching 探测 - 请求1] dump.usage: {dump['usage']}")

    # 第二次请求：预期 cache_read_input_tokens > 0
    msgs_2 = []
    async for msg in query(prompt="说2", options=options):
        msgs_2.append(msg)
        if hasattr(msg, "usage"):
            print(f"\n[Caching 探测 - 请求2] usage: {msg.usage}")

    print("\n[Caching 结论] 请检查上方输出:")
    print("  - 如果看到 cache_creation_input_tokens → SDK 原生支持 Caching ✅")
    print("  - 如果没有任何 cache 字段 → 需要手动注入 cache_control header ⚠️")
    print("  - 如果 SDK 不暴露 usage → 需要通过 API Dashboard 验证 🔍")

    assert len(msgs_1) > 0  # 至少完成了请求
```

**Step 1 交付物**:
- `uv sync` 成功
- `uv run pytest tests/test_smoke.py -v -s` 全部通过
- **关键**: 拿到 Caching 探测结果和 Block 类型探测结果，记录到文档

---

### Step 2: BaseAgent 纯函数开发（~3h）

#### 2.1 定义 HarnessState

```python
# src/harness_agent/core/state.py
"""LangGraph 状态定义"""
from typing import TypedDict, Annotated
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class HarnessState(TypedDict):
    """LangGraph 全局状态

    ⚠️ 关于"脑裂"的设计决策（v3 明确）：
    ─────────────────────────────────────────
    messages 字段仅作为 Agent 执行结果的「归档记录」。
    Agent 内部的 ReAct 循环（思考→工具调用→观察→再思考）
    完全由 claude-agent-sdk 内部的状态机闭环管理。

    LangGraph State 和 SDK 内部状态是两个独立的世界。
    在 Phase 1 中，我们不试图统一它们。
    在 Phase 2 的 Harness 重试循环中，我们通过
    「将上一轮的归档摘要注入到下一轮的 prompt」来桥接。
    ─────────────────────────────────────────

    Attributes:
        messages: 对话归档（add_messages reducer 自动追加）
        task:     用户原始任务描述
        project_dir: 项目工作目录
    """
    messages: Annotated[list[BaseMessage], add_messages]
    task: str
    project_dir: str
```

#### 2.2 BaseAgent（全类型事件发射）

```python
# src/harness_agent/core/agents/base_agent.py
"""Agent 基类 — 纯函数，全类型事件发射，无 IO 副作用"""
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)
from langchain_core.messages import AIMessage
from langchain_core.callbacks import adispatch_custom_event

from harness_agent.core.state import HarnessState


_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。"""


class BaseAgent:
    """LangGraph 节点 — 纯函数语义

    核心职责：
    1. 调用 claude-agent-sdk 执行任务
    2. 对 SDK 返回的每种 Block 类型发射不同的自定义事件
    3. 组装完整结果，返回 State update

    绝不做的事：
    - ❌ print() / sys.stdout.write()
    - ❌ 直接操作终端
    - ❌ 管理对话历史（SDK 内部闭环）
    """

    def __init__(
        self,
        name: str = "default",
        system_prompt: str = _BASE_SYSTEM_PROMPT,
        allowed_tools: list[str] | None = None,
        max_turns: int = 15,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]
        self.max_turns = max_turns

    def _build_options(self, project_dir: str) -> ClaudeAgentOptions:
        """构建 SDK 配置"""
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            cwd=project_dir,
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            permission_mode="acceptEdits",
        )

    async def __call__(self, state: HarnessState) -> dict:
        """LangGraph 节点入口

        ⚠️ 关于状态管理的"脑裂"：
        我们只从 state 中取 task 和 project_dir。
        state["messages"] 中的历史记录不会被喂给 SDK。
        SDK 内部有自己的 ReAct 状态机，负责工具调用循环。
        LangGraph 的 messages 仅用于归档最终结果。
        """
        task = state["task"]
        project_dir = state.get("project_dir", ".")
        options = self._build_options(project_dir)

        collected_texts: list[str] = []
        tool_calls: list[dict] = []

        async for message in query(prompt=task, options=options):

            # ── AssistantMessage: 模型的思考和工具调用意图 ──
            if isinstance(message, AssistantMessage):
                for block in message.content:

                    # 文本块：模型在"说话"
                    if isinstance(block, TextBlock):
                        collected_texts.append(block.text)
                        await adispatch_custom_event(
                            "agent_text",
                            {
                                "agent": self.name,
                                "text": block.text,
                            },
                        )

                    # 工具调用块：模型在"动手"
                    elif isinstance(block, ToolUseBlock):
                        tool_info = {
                            "agent": self.name,
                            "tool_name": block.name,
                            "tool_id": block.id,
                            "tool_input": _summarize_tool_input(block.input),
                        }
                        tool_calls.append(tool_info)
                        await adispatch_custom_event(
                            "agent_tool",
                            tool_info,
                        )

            # ── ResultMessage: 工具执行结果 ──
            elif isinstance(message, ResultMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        result_text = ""
                        if isinstance(block.content, str):
                            result_text = block.content
                        elif isinstance(block.content, list):
                            # content 可能是 [{"type": "text", "text": "..."}]
                            for item in block.content:
                                if isinstance(item, dict) and "text" in item:
                                    result_text += item["text"]

                        is_error = getattr(block, "is_error", False)
                        await adispatch_custom_event(
                            "agent_result",
                            {
                                "agent": self.name,
                                "tool_use_id": block.tool_use_id,
                                "content": result_text[:500],  # 截断防爆
                                "is_error": is_error,
                            },
                        )

                        if is_error:
                            await adispatch_custom_event(
                                "agent_error",
                                {
                                    "agent": self.name,
                                    "error": result_text[:500],
                                },
                            )

        # ── 组装最终归档结果 ──
        summary_parts = []
        if collected_texts:
            summary_parts.append("\n".join(collected_texts))
        if tool_calls:
            tool_summary = ", ".join(
                f"{t['tool_name']}({t['tool_input']})" for t in tool_calls
            )
            summary_parts.append(f"\n[工具调用记录] {tool_summary}")

        response_text = (
            "\n".join(summary_parts) if summary_parts
            else "[Agent 执行完毕，无文本输出]"
        )

        return {
            "messages": [AIMessage(content=response_text, name=self.name)]
        }


def _summarize_tool_input(tool_input: dict) -> str:
    """将工具输入摘要化，避免超长内容污染日志"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:100]

    # 常见的工具输入字段
    if "command" in tool_input:
        return tool_input["command"][:100]
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "content" in tool_input:
        return f"[{len(tool_input['content'])} chars]"
    return str(tool_input)[:100]
```

**Step 2 交付物**: BaseAgent 能发射 `agent_text`, `agent_tool`, `agent_result`, `agent_error` 四种事件。

---

### Step 3: LangGraph 极简编排引擎（~2h）

```python
# src/harness_agent/core/orchestrator.py
"""LangGraph 编排引擎 — Phase 1 极简直线图"""
from langgraph.graph import StateGraph, END
from harness_agent.core.state import HarnessState
from harness_agent.core.agents.base_agent import BaseAgent


def build_graph() -> StateGraph:
    """Phase 1 状态图: START → default_agent → END"""
    default_agent = BaseAgent(name="default")

    graph = StateGraph(HarnessState)
    graph.add_node("default_agent", default_agent)
    graph.set_entry_point("default_agent")
    graph.add_edge("default_agent", END)

    return graph


class HarnessOrchestrator:
    """编排器封装"""

    def __init__(self, project_dir: str = "."):
        self.project_dir = project_dir
        self.graph = build_graph().compile()

    async def run(self, task: str) -> dict:
        """同步执行（非流式），用于测试"""
        initial_state: HarnessState = {
            "messages": [],
            "task": task,
            "project_dir": self.project_dir,
        }
        return await self.graph.ainvoke(initial_state)

    async def stream(self, task: str):
        """流式执行，yield astream_events 事件"""
        initial_state: HarnessState = {
            "messages": [],
            "task": task,
            "project_dir": self.project_dir,
        }
        async for event in self.graph.astream_events(
            initial_state, version="v2"
        ):
            yield event
```

**Step 3 交付物**: 直线图编译通过，`run()` 和 `stream()` 均可正常调用。

---

### Step 4: CLI 流式渲染层（~3h）

#### 4.1 事件渲染器

```python
# src/harness_agent/cli.py
"""CLI 入口 — 分类事件渲染层"""
import asyncio
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from harness_agent.core.orchestrator import HarnessOrchestrator

load_dotenv()
console = Console()


# ── 事件渲染器 ──────────────────────────────────────────

class TerminalRenderer:
    """将 astream_events 中的自定义事件渲染到终端

    事件类型与渲染规则：
    ┌──────────────┬─────────────────────────────────────┐
    │ agent_text   │ 直接打印文本（流式追加）              │
    │ agent_tool   │ 打印 🛠️ 工具名 + 摘要输入            │
    │ agent_result │ 打印 ✅ 成功 或 ❌ 失败 + 截断内容    │
    │ agent_error  │ 打印 💥 错误详情                     │
    └──────────────┴─────────────────────────────────────┘
    """

    def __init__(self):
        self.tool_count = 0

    def handle_event(self, event: dict):
        kind = event.get("event")
        name = event.get("name", "")

        if kind != "on_custom_event":
            return

        data = event.get("data", {})

        if name == "agent_text":
            console.print(data.get("text", ""), end="")

        elif name == "agent_tool":
            self.tool_count += 1
            tool_name = data.get("tool_name", "unknown")
            tool_input = data.get("tool_input", "")
            console.print(
                f"\n  [dim]🛠️  [{self.tool_count}] {tool_name}[/dim]"
                f" [dim italic]{tool_input}[/dim italic]"
            )

        elif name == "agent_result":
            is_error = data.get("is_error", False)
            content = data.get("content", "")[:200]
            if is_error:
                console.print(f"  [red]❌ 失败:[/red] [dim]{content}[/dim]")
            else:
                console.print(f"  [green]✅ 完成[/green]")

        elif name == "agent_error":
            error = data.get("error", "")[:300]
            console.print(f"\n  [bold red]💥 错误:[/bold red] {error}")


# ── CLI 命令 ────────────────────────────────────────────

@click.group()
@click.version_option(version="0.1.0")
def cli():
    """🐴 Harness Agent — 驾驭工程实践"""
    pass


@cli.command()
@click.argument("prompt")
@click.option(
    "--project", "-p",
    default=".",
    type=click.Path(exists=True),
    help="项目工作目录",
)
def run(prompt: str, project: str):
    """执行一个开发任务

    示例: harness run "创建一个 hello_world.py"
    """
    project_dir = str(Path(project).resolve())

    console.print(Panel(
        f"[bold cyan]任务:[/] {prompt}\n[bold cyan]项目:[/] {project_dir}",
        title="🐴 Harness Agent",
        border_style="cyan",
    ))

    asyncio.run(_run_streaming(prompt, project_dir))


async def _run_streaming(prompt: str, project_dir: str):
    """核心流式渲染循环"""
    orchestrator = HarnessOrchestrator(project_dir=project_dir)
    renderer = TerminalRenderer()

    console.print("\n[dim]Agent 正在工作...[/dim]\n")

    async for event in orchestrator.stream(prompt):
        renderer.handle_event(event)

    # 渲染收尾
    console.print("\n")
    if renderer.tool_count > 0:
        console.print(
            f"[dim]共执行了 {renderer.tool_count} 次工具调用[/dim]"
        )
    console.print("[bold green]✅ 任务完成[/bold green]\n")


@cli.command()
def version():
    """显示版本信息"""
    console.print("[bold]Harness Agent[/bold] v0.1.0")
    console.print("[dim]驾驭工程实践 — Powered by Claude Agent SDK[/dim]")


if __name__ == "__main__":
    cli()
```

#### 4.2 终端输出效果预期

```
┌───────────────── 🐴 Harness Agent ──────────────────┐
│ 任务: 重构 user.py，将 ORM 查询提取到 Repository 层   │
│ 项目: E:\projects\my-app                             │
└─────────────────────────────────────────────────────┘

Agent 正在工作...

我来分析一下 user.py 的当前结构...

  🛠️  [1] Read  user.py
  ✅ 完成

发现了 3 处直接的 ORM 查询，我来创建 Repository 层...

  🛠️  [2] Write  repositories/user_repository.py
  ✅ 完成
  🛠️  [3] Edit  user.py
  ✅ 完成
  🛠️  [4] Bash  python -m pytest tests/test_user.py
  ✅ 完成

重构完成！已将所有 ORM 查询迁移到 UserRepository 类中。

共执行了 4 次工具调用
✅ 任务完成
```

**Step 4 交付物**: 终端能分类显示文本思考、工具调用、执行结果、错误信息。

---

## ✅ Phase 1 验收标准

| # | 验收项 | 通过条件 |
|---|--------|---------|
| 1 | **SDK 连通** | 冒烟测试全部通过，Block 类型探测结果已记录 |
| 2 | **Caching 验证** | 冒烟测试输出已判读，Caching 策略已确定（生效/需手动/不支持） |
| 3 | **编排正确** | 直线图 START→Agent→END 执行正常 |
| 4 | **文本可见** | CLI 能实时显示 Agent 的思考文本 |
| 5 | **工具可见** | CLI 能实时显示每次工具调用的名称和摘要 |
| 6 | **结果可见** | CLI 能显示工具的成功/失败状态 |
| 7 | **纯函数** | BaseAgent 内部无任何 `print()` 调用 |
| 8 | **无 Router** | 状态图只有 1 个节点 |

---

## ⚠️ 已知风险与应对

| # | 风险 | 影响 | 应对 |
|---|------|------|------|
| 1 | **SDK 的 query() 不发射 LangChain 兼容事件** | `astream_events` 无法直接捕获底层流 | 已解决：通过 `adispatch_custom_event` 手动桥接 |
| 2 | **LangGraph-SDK 脑裂** | Phase 2 的重试循环中 Agent 可能"失忆" | 已明确：Phase 1 中 State.messages 仅归档，Phase 2 通过"摘要注入 prompt"桥接 |
| 3 | **Prompt Caching 不生效** | 调试阶段 token 浪费 | 已解决：冒烟测试强制探测，根据结果决定策略 |
| 4 | **ToolUseBlock / ToolResultBlock 的实际类名与文档不一致** | 事件分类逻辑报错 | 已解决：Step 1.5 的类型探测测试会暴露实际结构 |
| 5 | **uv 在 Windows 上的兼容性** | 安装失败 | 回退到 `pip install -e .` |

---

## 📊 时间预估

| Step | 内容 | 预估时间 |
|------|------|---------|
| Step 1 | 骨架 + 环境 + 冒烟测试（含 Caching 探测） | ~2h |
| Step 2 | BaseAgent 全类型事件发射 | ~3h |
| Step 3 | LangGraph 直线图 | ~2h |
| Step 4 | CLI 分类渲染 + 终端体验打磨 | ~3h |
| | **合计** | **~10h** |
