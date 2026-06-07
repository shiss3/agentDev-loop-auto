# Phase 2: CLI Chat 交互模式 + 上下文集成

> **版本**: v1.1 | **日期**: 2026-06-07 | **前置**: Phase 1 已完成
> **v1.1 修正**: prompt_async 替换 / ChatSession 过渡态标记 / 动态上下文热更新 / Markdown Buffer 策略

---

## 🎯 阶段目标

**一句话**: 将单次执行的 `harness run` 升级为多轮交互的 `harness chat` 聊天模式，直接监听 `claude-agent-sdk` 事件流，提供沉浸式终端交互体验。

**做的事**（Phase 2 范围）:
- ✅ CLI Chat REPL 聊天循环
- ✅ 直接监听 SDK 事件流，绕过 LangGraph astream_events
- ✅ 增强终端渲染（Markdown / Spinner / 工具折叠 / Token 统计）
- ✅ 集成共享上下文与缓存方案（方案由用户提供，本阶段做接口对接）
- ✅ 会话生命周期管理（创建 / 多轮 / 退出）
- ✅ 斜杠命令系统

**不做的事**（明确排除）:
- ❌ LangGraph 多 Agent 编排扩展（后续阶段）
- ❌ 共享上下文/缓存方案本身的设计（用户提供）
- ❌ Docker 沙箱 / Hooks 审查（Phase 3）
- ❌ Web / Flutter 前端（Phase 5）

---

## 🏗️ 架构设计

### Phase 1 → Phase 2 核心变化

| 维度 | Phase 1 | Phase 2 |
|------|---------|---------|
| **入口** | `harness run "prompt"` 单次执行 | `harness chat` 多轮 REPL |
| **SDK 调用** | `query()` 无状态单次查询 | `ClaudeSDKClient` 有状态会话 |
| **事件管道** | LangGraph `astream_events` → `adispatch_custom_event` | SDK 原生事件流直接消费 |
| **渲染** | 简单 print，无状态 | Rich Live + Markdown + Spinner + 统计 |
| **对话状态** | 无（每次全新） | ChatSession 管理多轮对话 |
| **上下文** | 硬编码 system_prompt | 接入共享上下文管理器（用户方案） |

### 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│  harness chat --project ./my-app                                │
│                                                                 │
│  用户 REPL 循环                                                  │
│  ┌───────────────────────────────────────────────────────┐      │
│  │  You > 重构 user.py，将 ORM 查询提取到 Repository 层    │      │
│  │  You > /context show                                   │      │
│  │  You > /exit                                           │      │
│  └────────────────────────┬──────────────────────────────┘      │
└───────────────────────────┼─────────────────────────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  ChatCLI  (chat/repl.py)   │
              │                            │
              │  - REPL 主循环              │
              │  - 斜杠命令解析/分发         │
              │  - 输入预处理               │
              │  - 调度 ChatSession         │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  ChatSession               │
              │  (chat/session.py)         │
              │                            │
              │  - 包装 ClaudeSDKClient     │
              │  - 多轮会话状态管理          │
              │  - SDK Message → ChatEvent │
              │  - 注入共享上下文            │
              │  - yield ChatEvent 事件流   │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  ChatRenderer              │
              │  (chat/renderer.py)        │
              │                            │
              │  - 消费 ChatEvent 流        │
              │  - Rich Live 动态渲染       │
              │  - Markdown 文本渲染        │
              │  - 工具调用折叠/展开         │
              │  - Spinner 思考动画         │
              │  - Token 用量统计           │
              │  - 耗时计时                 │
              └────────────────────────────┘
```

### 关键设计原则

| # | 原则 | 说明 |
|---|------|------|
| 1 | **SDK 直连（过渡态）** | Chat 模式绕过 LangGraph，直接用 ClaudeSDKClient 管理会话。**⚠️ ChatSession 是过渡态组件**：Phase 3 引入多 Agent 路由时，多轮对话管理权将交还给 LangGraph StateGraph + Checkpointer。当前设计中 ChatSession 的事件流协议（`ChatEvent`）和渲染层（`ChatRenderer`）是长期资产，SDK 封装层则会被 LangGraph 节点替代。 |
| 2 | **事件驱动** | 定义 `ChatEvent` 统一事件类型，Session 只产出事件，Renderer 只消费事件，职责清晰。**这是跨 Phase 的长期协议。** |
| 3 | **渲染分离** | ChatSession 绝不 print。所有终端输出由 ChatRenderer 独占。 |
| 4 | **上下文可插拔 + 可热更新** | 共享上下文通过 `ContextProvider` 协议注入 ChatSession。当上下文发生关键变更时（如新 .skills 沉淀、项目切换），ContextProvider 通过 `has_context_changed()` 信号触发 ChatSession 平滑重启底层 Client，并将历史摘要灌入新 Client 防止"失忆"。方案由用户提供，本阶段只定义接口。 |
| 5 | **向后兼容** | 保留 `harness run` 命令不变，Phase 1 的 LangGraph 管道继续可用。 |

---

## 📁 目录结构变更

```
src/harness_agent/
├── __init__.py                         # 不变
├── cli.py                              # 修改：新增 chat 命令入口
├── config.py                           # 修改：新增 chat 相关配置项
├── chat/                               # ★ 新增：Chat 模块
│   ├── __init__.py
│   ├── repl.py                         # REPL 主循环 + 输入处理
│   ├── session.py                      # ChatSession（包装 ClaudeSDKClient）
│   ├── renderer.py                     # 增强事件渲染器
│   ├── events.py                       # ChatEvent 事件类型定义
│   └── commands.py                     # 斜杠命令注册与分发
├── context/                            # ★ 新增：上下文集成层
│   ├── __init__.py
│   └── provider.py                     # ContextProvider 协议（接口定义）
└── core/                               # 不变（Phase 1 保留）
    ├── orchestrator.py
    ├── state.py
    └── agents/
        └── base_agent.py
```

---

## 📅 任务分解

### Step 1: ChatEvent 事件类型定义（~1h）

> 统一的事件协议，连接 Session 层和 Renderer 层。

```python
# src/harness_agent/chat/events.py
"""Chat 事件类型定义 — Session 与 Renderer 之间的协议"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """事件类型枚举"""

    # ── Agent 输出 ──
    TEXT = "text"                    # 模型文本输出（思考/回答）
    TOOL_USE = "tool_use"           # 工具调用开始
    TOOL_RESULT = "tool_result"     # 工具执行结果

    # ── 会话生命周期 ──
    TURN_START = "turn_start"       # 一轮对话开始（用户发送消息后）
    TURN_END = "turn_end"           # 一轮对话结束（Agent 完成回复）

    # ── 状态 ──
    THINKING = "thinking"           # Agent 正在思考（工具调用间隙）
    ERROR = "error"                 # 错误

    # ── 统计 ──
    USAGE = "usage"                 # Token 用量统计


@dataclass
class ChatEvent:
    """单个事件

    Session 层产出 → Renderer 层消费。
    所有终端渲染决策由 Renderer 根据 event.type 决定。

    Attributes:
        type:      事件类型
        data:      事件数据（结构由 type 决定）
        agent:     产出事件的 Agent 名称
        timestamp: 事件产生时间
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    agent: str = "default"
    timestamp: datetime = field(default_factory=datetime.now)


# ── 便捷工厂函数 ──────────────────────────────────────


def text_event(text: str, agent: str = "default") -> ChatEvent:
    return ChatEvent(type=EventType.TEXT, data={"text": text}, agent=agent)


def tool_use_event(
    tool_name: str,
    tool_id: str,
    tool_input: str,
    agent: str = "default",
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TOOL_USE,
        data={
            "tool_name": tool_name,
            "tool_id": tool_id,
            "tool_input": tool_input,
        },
        agent=agent,
    )


def tool_result_event(
    tool_use_id: str,
    content: str,
    is_error: bool = False,
    agent: str = "default",
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TOOL_RESULT,
        data={
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        },
        agent=agent,
    )


def turn_start_event(turn_number: int, prompt: str) -> ChatEvent:
    return ChatEvent(
        type=EventType.TURN_START,
        data={"turn_number": turn_number, "prompt": prompt},
    )


def turn_end_event(
    turn_number: int,
    tool_count: int,
    duration_ms: int,
) -> ChatEvent:
    return ChatEvent(
        type=EventType.TURN_END,
        data={
            "turn_number": turn_number,
            "tool_count": tool_count,
            "duration_ms": duration_ms,
        },
    )


def error_event(error: str, agent: str = "default") -> ChatEvent:
    return ChatEvent(
        type=EventType.ERROR, data={"error": error}, agent=agent
    )


def usage_event(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> ChatEvent:
    return ChatEvent(
        type=EventType.USAGE,
        data={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
    )
```

**Step 1 交付物**: `ChatEvent` + `EventType` + 工厂函数。Session 和 Renderer 可以独立开发。

---

### Step 2: ContextProvider 接口定义（~1h）

> 为用户后续提供的上下文/缓存方案预留可插拔接口。

```python
# src/harness_agent/context/provider.py
"""共享上下文提供者 — 可插拔接口

⚠️ 方案设计：
   上下文和缓存的具体实现方案由用户提供。
   本模块只定义 ChatSession 需要的接口协议。
   用户实现 ContextProvider 后注入 ChatSession 即可。

接口职责：
   1. build_system_prompt()   — 将上下文组装为 system_prompt 注入 SDK
   2. on_turn_end()           — Agent 完成一轮对话后更新上下文
   3. has_context_changed()   — 检测上下文是否发生关键变更（触发 Client 热重启）
   4. get_history_summary()   — 获取历史摘要（Client 重启时灌入防"失忆"）
   5. get_context_summary()   — 给 /context show 命令返回摘要

关于动态上下文热更新（v1.1 新增）：
   ─────────────────────────────────────────
   问题：ClaudeSDKClient 启动后，system_prompt 是固定的。
   但在多轮对话中上下文可能变化（新 .skills 沉淀、项目切换等），
   存活的 Client 实例无法动态热更新 System Prompt。

   解决方案：
   每轮对话结束后，ChatSession 调用 has_context_changed()。
   如果返回 True，ChatSession 平滑重启底层 Client：
     1. 调用 get_history_summary() 获取对话历史摘要
     2. 关闭旧 Client
     3. 用新的 system_prompt（含更新后的上下文）创建新 Client
     4. 将历史摘要作为首条消息灌入新 Client
   对用户完全透明，不中断聊天体验。
   ─────────────────────────────────────────
"""

from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextProvider(Protocol):
    """共享上下文提供者协议

    任何实现了这三个方法的类都可以作为 ChatSession 的上下文源。
    使用 Protocol 而非 ABC，允许鸭子类型。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> str:
        """将共享上下文注入 system_prompt

        Args:
            base_prompt:  BaseAgent 原始 system_prompt
            project_dir:  当前项目目录

        Returns:
            组装后的完整 system_prompt，包含：
            - 原始 base_prompt
            - 全局上下文（项目结构、技术栈等）
            - 领域上下文（前端/后端/接口）
            - 缓存的经验 (.skills)
            - 其他用户定义的上下文
        """
        ...

    def on_turn_end(
        self,
        prompt: str,
        response_summary: str,
        tool_calls: list[dict],
    ) -> None:
        """一轮对话结束后的回调 — 用于更新上下文

        Args:
            prompt:           用户本轮输入
            response_summary: Agent 回复摘要
            tool_calls:       本轮工具调用记录
        """
        ...

    def has_context_changed(self) -> bool:
        """检测上下文是否发生了关键变更
        
        ChatSession 在每轮对话结束后调用此方法。
        如果返回 True，ChatSession 将平滑重启底层 Client
        以应用最新的上下文。
        """
        ...

    def get_history_summary(
        self,
        collected_texts: list[str],
        tool_calls: list[dict],
        turn_count: int,
    ) -> str:
        """生成对话历史摘要 — 用于 Client 重启时防"失忆""""
        ...

    def get_context_summary(self) -> str:
        """返回当前上下文的摘要 — 供 /context show 命令使用"""
        ...


class DefaultContextProvider:
    """默认上下文提供者 — 透传原始 prompt，不注入任何上下文

    在用户提供具体实现之前作为占位符。
    """

    def build_system_prompt(self, base_prompt: str, project_dir: str) -> str:
        return base_prompt

    def on_turn_end(
        self,
        prompt: str,
        response_summary: str,
        tool_calls: list[dict],
    ) -> None:
        pass  # 默认不做任何事

    def has_context_changed(self) -> bool:
        return False  # 默认上下文永远不变

    def get_history_summary(
        self,
        collected_texts: list[str],
        tool_calls: list[dict],
        turn_count: int,
    ) -> str:
        return ""  # 默认无历史摘要

    def get_context_summary(self) -> str:
        return "[未配置共享上下文]"
```

**Step 2 交付物**: `ContextProvider` Protocol + `DefaultContextProvider` 占位实现。

---

### Step 3: ChatSession — SDK 会话封装（~4h）

> 核心模块。包装 `ClaudeSDKClient`，管理多轮对话，产出 `ChatEvent` 事件流。

```python
# src/harness_agent/chat/session.py
"""ChatSession — 多轮对话会话管理

核心职责：
1. 包装 ClaudeSDKClient，管理会话生命周期
2. 将 SDK 的 Message/Block 转换为 ChatEvent 事件流
3. 注入共享上下文到 system_prompt
4. 维护对话统计（轮次 / Token / 工具调用数）
5. 检测上下文变更，平滑重启底层 Client（v1.1）

⚠️ 过渡态说明：
   ChatSession 是 Phase 2 的单 Agent 封装，Phase 3 中
   多轮对话管理权将交还给 LangGraph StateGraph + Checkpointer。
   此时 ChatSession 退役，但以下部分会被复用：
   - ChatEvent 事件协议 → LangGraph 节点也产出 ChatEvent
   - ChatRenderer → 继续消费 ChatEvent
   - ContextProvider → 继续通过 system_prompt 注入上下文

绝不做的事：
- ❌ print() / 终端输出（交给 Renderer）
- ❌ 管理用户输入（交给 REPL）
- ❌ 设计上下文方案（交给 ContextProvider）
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from harness_agent.chat.events import (
    ChatEvent,
    text_event,
    tool_use_event,
    tool_result_event,
    turn_start_event,
    turn_end_event,
    error_event,
    usage_event,
)
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


_BASE_SYSTEM_PROMPT = """你是 Harness Agent 系统中的通用开发助手。
你可以读写文件、执行命令来完成用户的开发任务。
请直接动手完成任务，不要只给建议。"""


class SessionStats:
    """会话统计"""

    def __init__(self):
        self.turn_count: int = 0
        self.total_tool_calls: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_tool_calls": self.total_tool_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }


class ChatSession:
    """多轮对话会话

    使用方式：

        session = ChatSession(project_dir="./my-app")
        await session.start()

        # 多轮对话
        async for event in session.send("重构 user.py"):
            renderer.handle(event)

        async for event in session.send("再加个单元测试"):
            renderer.handle(event)

        await session.close()
    """

    def __init__(
        self,
        project_dir: str = ".",
        model: str | None = None,
        max_turns: int = 30,
        context_provider: ContextProvider | None = None,
        allowed_tools: list[str] | None = None,
    ):
        self.project_dir = project_dir
        self.model = model
        self.max_turns = max_turns
        self.context_provider = context_provider or DefaultContextProvider()
        self.allowed_tools = allowed_tools or ["Read", "Write", "Edit", "Bash"]

        self._client: ClaudeSDKClient | None = None
        self.stats = SessionStats()
        self._is_active = False
        self._all_collected_texts: list[str] = []
        self._all_tool_calls: list[dict] = []

    @property
    def is_active(self) -> bool:
        return self._is_active

    def _build_options(self) -> ClaudeAgentOptions:
        """构建 SDK 配置，注入共享上下文"""
        # 通过 ContextProvider 组装 system_prompt
        system_prompt = self.context_provider.build_system_prompt(
            base_prompt=_BASE_SYSTEM_PROMPT,
            project_dir=self.project_dir,
        )

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=self.project_dir,
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            permission_mode="acceptEdits",
        )

        if self.model:
            opts.model = self.model

        return opts

    async def start(self) -> None:
        """启动会话 — 初始化 ClaudeSDKClient"""
        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        self._is_active = True

    async def close(self) -> None:
        """关闭会话 — 清理资源"""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
        self._is_active = False

    async def _hot_restart_client(self) -> None:
        """v1.1: 平滑重启底层 Client — 应用最新上下文"""
        history_summary = self.context_provider.get_history_summary(
            collected_texts=self._all_collected_texts,
            tool_calls=self._all_tool_calls,
            turn_count=self.stats.turn_count,
        )

        if self._client:
            await self._client.__aexit__(None, None, None)

        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()

        if history_summary:
            await self._client.query(
                f"[系统] 以下是之前对话的摘要，请基于此继续：\n{history_summary}"
            )
            async for _ in self._client.receive_response():
                pass

    async def send(self, prompt: str) -> AsyncIterator[ChatEvent]:
        """发送一条消息，返回事件流

        这是 ChatSession 的核心方法。
        将用户输入发送给 SDK，将 SDK 返回的 Message
        逐一转换为 ChatEvent 并 yield。

        Args:
            prompt: 用户输入的文本

        Yields:
            ChatEvent: 供 Renderer 消费的事件流
        """
        if not self._client or not self._is_active:
            yield error_event("会话未启动，请先调用 start()")
            return

        self.stats.turn_count += 1
        turn_number = self.stats.turn_count
        turn_tool_count = 0
        collected_texts: list[str] = []
        tool_calls: list[dict] = []
        start_time = time.monotonic()

        # ── TURN_START ──
        yield turn_start_event(turn_number, prompt)

        try:
            await self._client.query(prompt)

            async for message in self._client.receive_response():

                # ── AssistantMessage ──
                if isinstance(message, AssistantMessage):
                    for block in message.content:

                        if isinstance(block, TextBlock):
                            collected_texts.append(block.text)
                            yield text_event(block.text)

                        elif isinstance(block, ToolUseBlock):
                            turn_tool_count += 1
                            input_summary = _summarize_tool_input(block.input)
                            tool_info = {
                                "tool_name": block.name,
                                "tool_id": block.id,
                                "tool_input": input_summary,
                            }
                            tool_calls.append(tool_info)
                            yield tool_use_event(
                                tool_name=block.name,
                                tool_id=block.id,
                                tool_input=input_summary,
                            )

                # ── ResultMessage ──
                elif isinstance(message, ResultMessage):
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", [])
                        err_text = "\n".join(errors) if errors else "未知错误"
                        yield error_event(err_text[:500])
                    else:
                        # 尝试提取 usage 信息
                        usage = getattr(message, "usage", None)
                        if usage:
                            yield usage_event(
                                input_tokens=getattr(usage, "input_tokens", 0),
                                output_tokens=getattr(usage, "output_tokens", 0),
                                cache_read_tokens=getattr(
                                    usage, "cache_read_input_tokens", 0
                                ),
                                cache_creation_tokens=getattr(
                                    usage, "cache_creation_input_tokens", 0
                                ),
                            )

        except Exception as e:
            yield error_event(f"SDK 异常: {str(e)}")
            collected_texts.append(f"[错误] {str(e)}")

        # ── 更新统计 + 历史记录 ──
        self.stats.total_tool_calls += turn_tool_count
        self._all_collected_texts.extend(collected_texts)
        self._all_tool_calls.extend(tool_calls)
        duration_ms = int((time.monotonic() - start_time) * 1000)

        # ── 通知 ContextProvider 更新上下文 ──
        response_summary = "\n".join(collected_texts)[:2000]
        self.context_provider.on_turn_end(
            prompt=prompt,
            response_summary=response_summary,
            tool_calls=tool_calls,
        )

        # ── v1.1: 检测上下文变更，触发 Client 热重启 ──
        if self.context_provider.has_context_changed():
            await self._hot_restart_client()

        # ── TURN_END ──
        yield turn_end_event(
            turn_number=turn_number,
            tool_count=turn_tool_count,
            duration_ms=duration_ms,
        )


def _summarize_tool_input(tool_input: dict) -> str:
    """工具输入摘要化（复用 Phase 1 逻辑）"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:100]
    if "command" in tool_input:
        return tool_input["command"][:100]
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "content" in tool_input:
        return f"[{len(tool_input['content'])} chars]"
    return str(tool_input)[:100]
```

#### 3.1 ClaudeSDKClient 不可用时的降级方案

如果 `ClaudeSDKClient` 的多轮 API 在实际测试中行为不符合预期，退化为逐次 `query()` 调用：

```python
# session.py 中的降级实现（备选）

async def send_fallback(self, prompt: str) -> AsyncIterator[ChatEvent]:
    """降级方案：用 query() 逐次调用

    每轮重新构建 options，通过 ContextProvider 注入
    上一轮的摘要来模拟多轮上下文。
    """
    from claude_agent_sdk import query

    options = self._build_options()
    # ... 与 Phase 1 的 BaseAgent.__call__ 类似，
    #     但产出 ChatEvent 而非 adispatch_custom_event
```

**Step 3 交付物**: `ChatSession` 能通过 `send()` 发送消息，yield `ChatEvent` 事件流。

---

### Step 4: ChatRenderer — 增强终端渲染器（~4h）

> 从 Phase 1 的简单 print 升级为沉浸式终端体验。

```python
# src/harness_agent/chat/renderer.py
"""ChatRenderer — 增强终端渲染器

Phase 1 的 TerminalRenderer 做了什么：
  - 简单 console.print 文本
  - 基本的工具名展示

Phase 2 的 ChatRenderer 新增：
  - Rich Markdown 渲染 Agent 文本（Buffer 策略）
  - Spinner 动画（Agent 思考时）
  - 工具调用折叠显示（名称 + 状态图标，输入可展开）
  - Turn 起止分隔线
  - Token 用量统计面板
  - 累计耗时

v1.1 Markdown Buffer 策略：
  ─────────────────────────────────────────
  问题：如果 SDK 分块流式吐出文本（chunk-by-chunk），
  半截 Markdown 丢给 Rich 渲染会导致格式错乱和终端闪烁。

  解决方案（两阶段渲染）：
  1. 流式阶段：每收到 TEXT 事件，先以纯文本打印到终端
  2. 完成阶段：当 TURN_END 事件到来时，用完整的 Buffer 做一次 Markdown 精渲染
  ─────────────────────────────────────────
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.spinner import Spinner
from rich.live import Live
from rich.table import Table

from harness_agent.chat.events import ChatEvent, EventType


class ChatRenderer:
    """消费 ChatEvent 流，渲染到终端"""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.turn_tool_count = 0
        self.total_tool_count = 0
        self._current_text_buffer: list[str] = []
        self._spinner_live: Live | None = None
        self._has_streamed_text = False

    # ── 公共入口 ──

    def handle(self, event: ChatEvent) -> None:
        """分发事件到对应的渲染方法"""
        handler = {
            EventType.TEXT: self._render_text,
            EventType.TOOL_USE: self._render_tool_use,
            EventType.TOOL_RESULT: self._render_tool_result,
            EventType.TURN_START: self._render_turn_start,
            EventType.TURN_END: self._render_turn_end,
            EventType.THINKING: self._render_thinking,
            EventType.ERROR: self._render_error,
            EventType.USAGE: self._render_usage,
        }.get(event.type)

        if handler:
            handler(event)

    # ── 各事件类型的渲染实现 ──

    def _render_turn_start(self, event: ChatEvent) -> None:
        """渲染一轮对话的开始"""
        self.turn_tool_count = 0
        self._current_text_buffer = []
        self._has_streamed_text = False
        self._stop_spinner()
        self.console.print()
        # 启动思考动画
        self._start_spinner()

    def _render_text(self, event: ChatEvent) -> None:
        """渲染 Agent 文本输出 — Buffer 策略"""
        self._stop_spinner()
        text = event.data.get("text", "")
        self._current_text_buffer.append(text)

        # 流式阶段：纯文本即时输出
        self.console.print(text, end="", highlight=False)
        self._has_streamed_text = True

    def _render_tool_use(self, event: ChatEvent) -> None:
        """渲染工具调用 — 紧凑的单行显示"""
        self._stop_spinner()
        self.turn_tool_count += 1
        self.total_tool_count += 1

        tool_name = event.data.get("tool_name", "?")
        tool_input = event.data.get("tool_input", "")

        # 工具图标映射
        icon = _tool_icon(tool_name)

        self.console.print(
            f"  [dim]{icon} [{self.total_tool_count}] "
            f"[bold]{tool_name}[/bold][/dim]"
            f" [dim italic]{tool_input}[/dim italic]"
        )

        # 工具执行期间显示 spinner
        self._start_spinner("  执行中...")

    def _render_tool_result(self, event: ChatEvent) -> None:
        """渲染工具执行结果"""
        self._stop_spinner()
        is_error = event.data.get("is_error", False)
        content = event.data.get("content", "")[:200]

        if is_error:
            self.console.print(f"  [red]✗ 失败[/red] [dim]{content}[/dim]")
        else:
            self.console.print(f"  [green]✓ 完成[/green]")

    def _render_turn_end(self, event: ChatEvent) -> None:
        """渲染一轮对话结束 — Markdown 精渲染 + 统计信息"""
        self._stop_spinner()
        
        # Markdown 精渲染
        if self._has_streamed_text and self._current_text_buffer:
            full_text = "".join(self._current_text_buffer)
            if any(indicator in full_text for indicator in ["```", "# ", "**", "- ", "1. ", "| ", "> "]):
                self.console.print()
                self.console.print(Panel(Markdown(full_text), border_style="dim", expand=True, padding=(0, 1)))

        tool_count = event.data.get("tool_count", 0)
        duration_ms = event.data.get("duration_ms", 0)

        parts = []
        if tool_count > 0:
            parts.append(f"🛠️ {tool_count} 次工具调用")
        if duration_ms > 0:
            seconds = duration_ms / 1000
            parts.append(f"⏱️ {seconds:.1f}s")

        if parts:
            summary = " · ".join(parts)
            self.console.print(f"\n  [dim]{summary}[/dim]")

        self.console.print()

    def _render_thinking(self, event: ChatEvent) -> None:
        """渲染思考状态"""
        self._start_spinner()

    def _render_error(self, event: ChatEvent) -> None:
        """渲染错误"""
        self._stop_spinner()
        error = event.data.get("error", "未知错误")[:500]
        self.console.print(
            Panel(
                f"[red]{error}[/red]",
                title="💥 错误",
                border_style="red",
                expand=False,
            )
        )

    def _render_usage(self, event: ChatEvent) -> None:
        """渲染 Token 用量"""
        input_t = event.data.get("input_tokens", 0)
        output_t = event.data.get("output_tokens", 0)
        cache_r = event.data.get("cache_read_tokens", 0)
        cache_c = event.data.get("cache_creation_tokens", 0)

        parts = [f"📊 in={input_t} out={output_t}"]
        if cache_r > 0:
            parts.append(f"cache_read={cache_r}")
        if cache_c > 0:
            parts.append(f"cache_create={cache_c}")

        self.console.print(f"  [dim]{' '.join(parts)}[/dim]")

    # ── Spinner 管理 ──

    def _start_spinner(self, text: str = "  思考中...") -> None:
        """启动思考动画"""
        if self._spinner_live is not None:
            return  # 已经在转了
        spinner = Spinner("dots", text=text, style="dim")
        self._spinner_live = Live(spinner, console=self.console, refresh_per_second=10)
        self._spinner_live.start()

    def _stop_spinner(self) -> None:
        """停止思考动画"""
        if self._spinner_live is not None:
            self._spinner_live.stop()
            self._spinner_live = None

    # ── 辅助渲染 ──

    def render_welcome(self, project_dir: str) -> None:
        """渲染欢迎信息"""
        self.console.print(
            Panel(
                "[bold cyan]🐴 Harness Agent — Chat Mode[/bold cyan]\n\n"
                f"[dim]项目:[/dim] {project_dir}\n"
                "[dim]输入消息开始对话，输入 [bold]/help[/bold] 查看命令[/dim]",
                border_style="cyan",
                expand=False,
            )
        )
        self.console.print()

    def render_goodbye(self, stats: dict) -> None:
        """渲染退出信息"""
        turns = stats.get("turn_count", 0)
        tools = stats.get("total_tool_calls", 0)
        self.console.print()
        self.console.print(
            Panel(
                f"[dim]本次会话: {turns} 轮对话 · {tools} 次工具调用[/dim]",
                title="👋 再见",
                border_style="dim",
                expand=False,
            )
        )

    def render_command_result(self, text: str) -> None:
        """渲染斜杠命令结果"""
        self.console.print(f"  [dim]{text}[/dim]")


def _tool_icon(tool_name: str) -> str:
    """工具名 → 图标"""
    icons = {
        "Read": "📖",
        "Write": "✏️",
        "Edit": "📝",
        "Bash": "⚡",
        "Grep": "🔍",
        "Glob": "📂",
    }
    return icons.get(tool_name, "🛠️")
```

#### 终端渲染效果预期

```
┌─ 🐴 Harness Agent — Chat Mode ─────────────────┐
│                                                  │
│ 项目: E:\projects\my-app                         │
│ 输入消息开始对话，输入 /help 查看命令              │
└──────────────────────────────────────────────────┘

You > 重构 user.py，将 ORM 查询提取到 Repository 层

  我来分析一下 user.py 的当前结构...

  📖 [1] Read  user.py
  ✓ 完成

  发现了 3 处直接的 ORM 查询，我来创建 Repository 层...

  ✏️ [2] Write  repositories/user_repository.py
  ✓ 完成
  📝 [3] Edit  user.py
  ✓ 完成
  ⚡ [4] Bash  python -m pytest tests/test_user.py
  ✓ 完成

  重构完成！已将所有 ORM 查询迁移到 UserRepository 类中。

  🛠️ 4 次工具调用 · ⏱️ 12.3s

You > 再加个单元测试覆盖边界情况

  好的，我来为 UserRepository 添加边界测试...
  ...
```

**Step 4 交付物**: `ChatRenderer` 支持全部 `EventType` 渲染，包含 Spinner / Markdown / Token 统计。

---

### Step 5: 斜杠命令系统（~2h）

```python
# src/harness_agent/chat/commands.py
"""斜杠命令注册与分发

用户在 REPL 中输入 / 开头的命令时，
由此模块解析并执行，不发送给 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from harness_agent.chat.repl import ChatCLI


@dataclass
class SlashCommand:
    """斜杠命令定义"""

    name: str
    description: str
    handler: Callable  # async def handler(cli: ChatCLI, args: str) -> None
    usage: str = ""


# ── 命令注册表 ──

_COMMANDS: dict[str, SlashCommand] = {}


def slash_command(name: str, description: str, usage: str = ""):
    """装饰器：注册斜杠命令"""

    def decorator(func):
        _COMMANDS[name] = SlashCommand(
            name=name,
            description=description,
            handler=func,
            usage=usage,
        )
        return func

    return decorator


def get_command(name: str) -> SlashCommand | None:
    return _COMMANDS.get(name)


def all_commands() -> dict[str, SlashCommand]:
    return _COMMANDS.copy()


# ── 内置命令实现 ──


@slash_command("help", "显示所有可用命令")
async def cmd_help(cli: ChatCLI, args: str) -> None:
    lines = ["[bold]可用命令:[/bold]\n"]
    for name, cmd in sorted(_COMMANDS.items()):
        usage_str = f" {cmd.usage}" if cmd.usage else ""
        lines.append(f"  [cyan]/{name}[/cyan]{usage_str}  — {cmd.description}")
    cli.renderer.render_command_result("\n".join(lines))


@slash_command("exit", "退出聊天")
async def cmd_exit(cli: ChatCLI, args: str) -> None:
    cli.request_exit()


@slash_command("quit", "退出聊天（同 /exit）")
async def cmd_quit(cli: ChatCLI, args: str) -> None:
    cli.request_exit()


@slash_command("clear", "清空当前会话，重新开始")
async def cmd_clear(cli: ChatCLI, args: str) -> None:
    await cli.reset_session()
    cli.renderer.render_command_result("✅ 会话已清空")


@slash_command("context", "显示当前共享上下文摘要", usage="[show]")
async def cmd_context(cli: ChatCLI, args: str) -> None:
    summary = cli.session.context_provider.get_context_summary()
    cli.renderer.render_command_result(summary)


@slash_command("stats", "显示当前会话统计")
async def cmd_stats(cli: ChatCLI, args: str) -> None:
    stats = cli.session.stats.to_dict()
    lines = [
        f"  对话轮次:    {stats['turn_count']}",
        f"  工具调用:    {stats['total_tool_calls']}",
        f"  输入 Token:  {stats['total_input_tokens']}",
        f"  输出 Token:  {stats['total_output_tokens']}",
    ]
    cli.renderer.render_command_result("\n".join(lines))


@slash_command("project", "显示或切换项目目录", usage="[path]")
async def cmd_project(cli: ChatCLI, args: str) -> None:
    if args.strip():
        # 切换项目目录（需要重建会话）
        new_dir = args.strip()
        cli.renderer.render_command_result(
            f"切换项目到 {new_dir}，会话将重建..."
        )
        await cli.switch_project(new_dir)
    else:
        cli.renderer.render_command_result(
            f"当前项目: {cli.session.project_dir}"
        )


@slash_command("model", "显示或切换模型", usage="[model_name]")
async def cmd_model(cli: ChatCLI, args: str) -> None:
    if args.strip():
        cli.renderer.render_command_result(
            f"切换模型到 {args.strip()}，会话将重建..."
        )
        await cli.switch_model(args.strip())
    else:
        current = cli.session.model or "default"
        cli.renderer.render_command_result(f"当前模型: {current}")
```

**Step 5 交付物**: 可扩展的斜杠命令系统，内置 7 个命令。

---

### Step 6: ChatCLI — REPL 主循环（~3h）

```python
# src/harness_agent/chat/repl.py
"""ChatCLI — REPL 聊天主循环

职责：
1. 管理输入循环（prompt → 解析 → 分发）
2. 区分普通消息和斜杠命令
3. 管理 ChatSession 的生命周期
4. 处理 Ctrl+C / Ctrl+D 等信号
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

from harness_agent.chat.session import ChatSession
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.commands import get_command, all_commands
from harness_agent.context.provider import ContextProvider, DefaultContextProvider


class ChatCLI:
    """REPL 聊天主循环

    使用方式（由 cli.py 的 chat 命令调用）：

        cli = ChatCLI(project_dir="./my-app")
        await cli.run()
    """

    def __init__(
        self,
        project_dir: str = ".",
        model: str | None = None,
        context_provider: ContextProvider | None = None,
        history_file: str | None = None,
    ):
        self.project_dir = str(Path(project_dir).resolve())
        self.model = model
        self.context_provider = context_provider or DefaultContextProvider()

        # 组件
        self.console = Console()
        self.renderer = ChatRenderer(console=self.console)
        self.session = self._create_session()

        # 输入历史
        history_path = history_file or str(
            Path.home() / ".harness" / "chat_history"
        )
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        self._prompt_session = PromptSession(
            history=FileHistory(history_path),
            auto_suggest=AutoSuggestFromHistory(),
        )

        # 状态
        self._should_exit = False

    def _create_session(self) -> ChatSession:
        return ChatSession(
            project_dir=self.project_dir,
            model=self.model,
            context_provider=self.context_provider,
        )

    async def run(self) -> None:
        """REPL 主循环"""
        self.renderer.render_welcome(self.project_dir)

        await self.session.start()

        try:
            while not self._should_exit:
                try:
                    # 获取用户输入
                    user_input = await self._get_input()

                    if user_input is None:
                        # Ctrl+D → 退出
                        break

                    user_input = user_input.strip()
                    if not user_input:
                        continue

                    # 判断是斜杠命令还是普通消息
                    if user_input.startswith("/"):
                        await self._handle_command(user_input)
                    else:
                        await self._handle_message(user_input)

                except KeyboardInterrupt:
                    # Ctrl+C → 中断当前，不退出
                    self.console.print("\n[dim]已中断[/dim]")
                    continue

        finally:
            await self.session.close()
            self.renderer.render_goodbye(self.session.stats.to_dict())

    async def _get_input(self) -> str | None:
        """获取用户输入（带历史 / 自动补全）"""
        try:
            result = await self._prompt_session.prompt_async("You > ")
            return result
        except EOFError:
            return None

    async def _handle_command(self, input_str: str) -> None:
        """处理斜杠命令"""
        # 解析: "/command arg1 arg2" → name="command", args="arg1 arg2"
        parts = input_str[1:].split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        command = get_command(name)
        if command:
            await command.handler(self, args)
        else:
            self.renderer.render_command_result(
                f"[yellow]未知命令: /{name}[/yellow]  输入 /help 查看可用命令"
            )

    async def _handle_message(self, prompt: str) -> None:
        """处理普通聊天消息 — 发送给 Agent"""
        async for event in self.session.send(prompt):
            self.renderer.handle(event)

    # ── 命令回调接口（供 commands.py 调用） ──

    def request_exit(self) -> None:
        self._should_exit = True

    async def reset_session(self) -> None:
        """清空会话，重建 ChatSession"""
        await self.session.close()
        self.session = self._create_session()
        await self.session.start()

    async def switch_project(self, new_dir: str) -> None:
        """切换项目目录"""
        self.project_dir = str(Path(new_dir).resolve())
        await self.reset_session()

    async def switch_model(self, model: str) -> None:
        """切换模型"""
        self.model = model
        await self.reset_session()
```

**Step 6 交付物**: 完整的 REPL 循环，支持输入历史、Ctrl+C/D、斜杠命令、消息发送。

---

### Step 7: CLI 入口集成（~1h）

```python
# src/harness_agent/cli.py — 修改部分（新增 chat 命令）

# 在现有 cli.py 末尾，run 命令和 version 命令之间，新增：

@cli.command()
@click.option(
    "--project", "-p",
    default=".",
    type=click.Path(exists=True),
    help="项目工作目录",
)
@click.option(
    "--model", "-m",
    default=None,
    help="指定模型（如 claude-sonnet-4-20250514）",
)
def chat(project: str, model: str):
    """启动交互式聊天模式

    示例:
        harness chat
        harness chat -p ./my-app
        harness chat -m claude-sonnet-4-20250514
    """
    from harness_agent.chat.repl import ChatCLI

    project_dir = str(Path(project).resolve())
    chat_cli = ChatCLI(project_dir=project_dir, model=model)
    asyncio.run(chat_cli.run())
```

#### pyproject.toml 新增依赖

```toml
# 在 dependencies 中新增：
"prompt-toolkit>=3.0.0",   # REPL 输入（历史、补全、快捷键）
```

**Step 7 交付物**: `harness chat` 命令可用，`harness run` 保持不变。

---

### Step 8: 配置扩展（~1h）

```python
# src/harness_agent/config.py — 新增 ChatConfig

@dataclass
class ChatConfig:
    """Chat 模式配置

    Attributes:
        max_turns:      单轮对话最大 Agent 交互轮次
        history_file:   输入历史文件路径
        show_usage:     是否显示 Token 用量
        show_timing:    是否显示耗时
        markdown_render: 是否启用 Markdown 渲染
    """

    max_turns: int = 30
    history_file: str | None = None
    show_usage: bool = True
    show_timing: bool = True
    markdown_render: bool = True


@dataclass
class HarnessConfig:
    """全局配置（扩展）"""

    project_dir: str = "."
    max_turns: int = 15
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash"]
    )
    log_level: str = "INFO"
    chat: ChatConfig = field(default_factory=ChatConfig)

    def __post_init__(self):
        self.project_dir = str(Path(self.project_dir).resolve())
```

**Step 8 交付物**: `ChatConfig` 可配置 Chat 模式行为。

---

### Step 9: 测试（~3h）

```python
# tests/test_chat_events.py
"""ChatEvent 事件类型测试"""

from harness_agent.chat.events import (
    ChatEvent, EventType,
    text_event, tool_use_event, turn_start_event, turn_end_event,
    error_event, usage_event,
)


def test_text_event_creation():
    event = text_event("hello")
    assert event.type == EventType.TEXT
    assert event.data["text"] == "hello"


def test_tool_use_event_creation():
    event = tool_use_event("Read", "id-1", "file.py")
    assert event.type == EventType.TOOL_USE
    assert event.data["tool_name"] == "Read"


def test_turn_lifecycle_events():
    start = turn_start_event(1, "test prompt")
    assert start.data["turn_number"] == 1

    end = turn_end_event(1, tool_count=3, duration_ms=1500)
    assert end.data["tool_count"] == 3
    assert end.data["duration_ms"] == 1500


def test_usage_event():
    event = usage_event(
        input_tokens=100, output_tokens=50,
        cache_read_tokens=80,
    )
    assert event.data["input_tokens"] == 100
    assert event.data["cache_read_tokens"] == 80


# tests/test_chat_renderer.py
"""ChatRenderer 渲染器测试"""

from io import StringIO
from rich.console import Console
from harness_agent.chat.renderer import ChatRenderer
from harness_agent.chat.events import (
    text_event, tool_use_event, turn_start_event,
    turn_end_event, error_event,
)


def _make_renderer() -> tuple[ChatRenderer, StringIO]:
    """创建带 StringIO 捕获的测试 renderer"""
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=80)
    renderer = ChatRenderer(console=console)
    return renderer, buffer


def test_text_render():
    renderer, buf = _make_renderer()
    renderer.handle(text_event("hello world"))
    output = buf.getvalue()
    assert "hello world" in output


def test_tool_render_increments_count():
    renderer, buf = _make_renderer()
    renderer.handle(tool_use_event("Read", "id-1", "file.py"))
    assert renderer.total_tool_count == 1
    renderer.handle(tool_use_event("Write", "id-2", "out.py"))
    assert renderer.total_tool_count == 2


def test_error_render():
    renderer, buf = _make_renderer()
    renderer.handle(error_event("something broke"))
    output = buf.getvalue()
    assert "something broke" in output


# tests/test_chat_commands.py
"""斜杠命令测试"""

from harness_agent.chat.commands import get_command, all_commands


def test_builtin_commands_registered():
    cmds = all_commands()
    assert "help" in cmds
    assert "exit" in cmds
    assert "clear" in cmds
    assert "context" in cmds
    assert "stats" in cmds


def test_get_unknown_command():
    assert get_command("nonexistent") is None


def test_get_known_command():
    cmd = get_command("help")
    assert cmd is not None
    assert cmd.name == "help"


# tests/test_context_provider.py
"""ContextProvider 接口测试"""

from harness_agent.context.provider import (
    ContextProvider, DefaultContextProvider,
)


def test_default_provider_passthrough():
    provider = DefaultContextProvider()
    result = provider.build_system_prompt("base", "/project")
    assert result == "base"


def test_default_provider_summary():
    provider = DefaultContextProvider()
    assert "未配置" in provider.get_context_summary()


def test_default_provider_context_never_changes():
    provider = DefaultContextProvider()
    assert provider.has_context_changed() is False

def test_default_provider_empty_history():
    provider = DefaultContextProvider()
    assert provider.get_history_summary([], [], 0) == ""

def test_protocol_compliance():
    """验证 DefaultContextProvider 实现了 ContextProvider 协议"""
    provider = DefaultContextProvider()
    assert isinstance(provider, ContextProvider)
```

**Step 9 交付物**: events / renderer / commands / provider 四个模块的单元测试全部通过。

---

## ✅ Phase 2 验收标准

| # | 验收项 | 通过条件 |
|---|--------|---------|
| 1 | **Chat 启动** | `harness chat` 进入 REPL，显示欢迎面板 |
| 2 | **多轮对话** | 连续发送多条消息，Agent 能理解上下文 |
| 3 | **文本渲染** | Agent 文本以 Markdown 格式渲染 |
| 4 | **工具可见** | 工具调用显示图标 + 名称 + 摘要，结果显示 ✓/✗ |
| 5 | **Spinner** | Agent 思考/工具执行时显示动画 |
| 6 | **统计面板** | 每轮结束显示工具调用数 + 耗时 |
| 7 | **斜杠命令** | `/help` `/exit` `/clear` `/stats` `/context` `/project` `/model` 均可用 |
| 8 | **输入历史** | 按 ↑↓ 可浏览历史输入 |
| 9 | **信号处理** | Ctrl+C 中断当前轮，Ctrl+D 退出 |
| 10 | **向后兼容** | `harness run "prompt"` 仍然可用（Phase 1 不受影响） |
| 11 | **上下文接口** | `ContextProvider` 协议定义完整，`DefaultContextProvider` 可正常占位 |
| 12 | **纯渲染分离** | `ChatSession` 无任何 print 调用 |
| 13 | **测试通过** | 所有新增测试通过 |

---

## ⚠️ 已知风险与应对

| # | 风险 | 影响 | 应对 |
|---|------|------|------|
| 1 | **ClaudeSDKClient 多轮 API 行为不明** | `client.query()` 连续调用是否保持上下文未验证 | Step 3 开始时先写探测测试；准备 `query()` 降级方案 |
| 2 | **SDK 的 Message 结构可能与 Phase 1 探测不同** | ChatSession 的事件转换逻辑可能需要调整 | 保持与 Phase 1 相同的 isinstance 判断，新类型走 fallback |
| 3 | **Rich Live/Spinner 与流式输出冲突** | Spinner 和文本输出可能交错 | 严格在 `_render_text` 前调用 `_stop_spinner` |
| 4 | **Windows 终端 emoji 显示** | 某些 Windows 终端不支持 emoji | 提供配置项关闭 emoji，回退到 ASCII 符号 |
| 5 | **用户尚未提供上下文方案** | Phase 2 可能在上下文集成部分阻塞 | `DefaultContextProvider` 做完整占位，不依赖用户方案即可完成其余功能 |
| 6 | **Client 热重启时的消息丢失** | 热重启期间如果用户恰好发送消息可能丢失 | 热重启发生在 `send()` 末尾、下一次 `_get_input()` 之前，不会与用户输入竞争 |
| 7 | **ChatSession 过渡态带来的 Phase 3 重构成本** | Phase 3 引入 LangGraph 多 Agent 时需要替换 ChatSession | 已明确标记过渡态；ChatEvent 协议和 ChatRenderer 是长期资产，只需替换事件源 |

---

## 📊 时间预估

| Step | 内容 | 预估时间 |
|------|------|---------:|
| Step 1 | ChatEvent 事件类型定义 | ~1h |
| Step 2 | ContextProvider 接口定义 | ~1h |
| Step 3 | ChatSession SDK 会话封装 | ~4h |
| Step 4 | ChatRenderer 增强终端渲染 | ~4h |
| Step 5 | 斜杠命令系统 | ~2h |
| Step 6 | ChatCLI REPL 主循环 | ~3h |
| Step 7 | CLI 入口集成 | ~1h |
| Step 8 | 配置扩展 | ~1h |
| Step 9 | 测试 | ~3h |
| | **合计** | **~20h** |

---

## 📋 执行顺序建议

```
Step 1 (events)  ──→  Step 2 (context) ──→  Step 3 (session) ──┐
                                                                │
Step 4 (renderer) ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ┘
     │
     ▼
Step 5 (commands) ──→  Step 6 (repl) ──→  Step 7 (cli) ──→ Step 8 (config)
                                                                │
                                                                ▼
                                                          Step 9 (tests)
```

> **关键路径**: events → session → renderer → repl → cli  
> **可并行**: Step 1+2 可同时进行；Step 5 可与 Step 4 并行

---

## 🔮 Phase 3 前瞻：LangGraph Checkpointer 与 ClaudeSDKClient 的整合

> 以下是对你提出的「Phase 3 如何处理 LangGraph 持久化状态与 SDK 内部会话之间的整合」问题的架构思考。

### 核心矛盾

Phase 3 引入多 Agent 路由后，系统中会同时存在两套状态机：

| 状态机 | 管理者 | 状态内容 | 持久化 |
|--------|--------|----------|--------|
| **图状态** | LangGraph StateGraph + Checkpointer | 路由决策、Agent 选择、重试计数、共享上下文 | ✅ 通过 Checkpointer 持久化 |
| **会话状态** | ClaudeSDKClient 内部 | ReAct 循环、工具调用栈、对话历史 | ❌ 仅内存，Client 关闭即丢失 |

这就是 Phase 1 文档中预警的「脑裂」问题的升级版。

### 整合策略：LangGraph 为主，SDK 为从

1. **SDK Client 降为短生命周期**：每个 LangGraph Agent 节点执行时，创建一个新的 `query()` 调用（不是 `ClaudeSDKClient` 长连接）。SDK 的内部会话状态不再需要跨节点保持。
2. **LangGraph 的 Checkpointer 接管持久化**：所有需要跨轮次、跨 Agent 保持的状态，都通过 `HarnessState` 的字段 + Checkpointer 来管理。
3. **上下文桥接**：每个 Agent 节点执行前，从 `state.agent_memory` 中提取上一轮的摘要，注入到 `query()` 的 prompt 中（而非依赖 SDK 内部的会话记忆）。
4. **ChatEvent 协议复用**：Agent 节点内部仍然产出 `ChatEvent`，通过 LangGraph 的 `adispatch_custom_event` 桥接到 Renderer。Phase 2 的 ChatRenderer 无需修改。

### Phase 2 → Phase 3 的迁移路径

| Phase 2 组件 | Phase 3 命运 | 说明 |
|-------------|-------------|------|
| `ChatEvent` | ✅ **保留** | 长期事件协议 |
| `ChatRenderer` | ✅ **保留** | 消费 ChatEvent |
| `ContextProvider` | ✅ **保留** | 继续提供 system_prompt 注入 |
| `ChatCLI` | ✅ **保留** | 输入循环不变，底层切到 LangGraph |
| `ChatSession` | ❌ **退役** | 被 LangGraph 替代 |
