# Harness Agent System — 完整项目规划方案

> 版本: v1.0 | 日期: 2026-06-06 | 状态: 规划阶段

---

## 1. 项目概述

### 1.1 项目名称

**Harness Agent** — 基于 Claude Agent SDK 的智能开发编排系统

### 1.2 项目目标

构建一个 Agent 编排系统，底层调用 `claude-agent-sdk`（非直接调用大模型），解决多终端独立开发时的上下文隔离、代码质量保障和经验沉淀问题。

### 1.3 核心痛点

| # | 痛点 | 场景 | 影响 |
|---|------|------|------|
| 1 | **多终端上下文隔离** | 前端 UI / 接口联调 / 后端业务分别开不同终端 | 各终端无法共享项目理解 |
| 2 | **上下文传递成本高** | 切换终端需要大量文字重新描述背景 | 效率低下，容易遗漏关键信息 |
| 3 | **缺少代码审查** | 习惯直接 `yes` 同意生成的代码 | 可能引入质量问题 |
| 4 | **经验无法沉淀** | 踩过的坑修好后没有系统性记录 | 同样的错误可能重复出现 |

### 1.4 解决思路

```
┌──────────────────────────────────────────────────────────┐
│  用户只需一个入口（CLI），Harness Agent 自动编排多个      │
│  Claude Agent SDK 实例，共享上下文、自动审查代码、         │
│  在沙箱中验证、沉淀经验到 .skills 文件                   │
└──────────────────────────────────────────────────────────┘
```

---

## 2. 技术栈

### 2.1 核心技术选型

| 层级 | 技术 | 说明 |
|------|------|------|
| **Agent 执行引擎** | `claude-agent-sdk` (Python) v0.2.93 | Anthropic 官方 Python SDK，`pip install claude-agent-sdk` |
| **Agent 编排** | LangGraph (Python) | 状态图编排，支持条件路由、并行执行 |
| **后端 API** | FastAPI + WebSocket | 提供 REST API 和实时状态推送 |
| **Web 前端** | React + TypeScript | Web 端仪表盘 |
| **跨端前端** | Flutter (Dart) | 移动端 / 桌面端跨平台客户端 |
| **沙箱环境** | Docker | 代码验证隔离执行 |
| **数据存储** | SQLite + 文件系统 | 会话历史、Skills 持久化 |
| **入口** | 独立 CLI | `harness start --project ./my-project` |

### 2.2 关于 claude-agent-sdk

Anthropic 官方已将 `claude-code-sdk` 正式升级并重命名为 `claude-agent-sdk`：

| 属性 | 值 |
|------|-----|
| PyPI 包名 | `claude-agent-sdk` |
| 最新版本 | 0.2.93 (2026-06-06) |
| Python 要求 | ≥ 3.10 |
| License | MIT |
| GitHub | https://github.com/anthropics/claude-agent-sdk-python（⭐7.2k） |
| 官方文档 | https://docs.anthropic.com/en/docs/claude-code/sdk |
| Claude CLI | SDK 内置打包，无需单独安装 |

**核心 API：**

- `query()` — 异步查询，返回 `AsyncIterator`，适合一次性任务
- `ClaudeSDKClient` — 双向交互式客户端，支持会话保持（推荐用于 Harness）
- `@tool` + `create_sdk_mcp_server()` — 自定义工具（In-Process MCP Server）
- `HookMatcher` + Hooks — 拦截 Agent 行为（PreToolUse / PostToolUse 等）
- `ClaudeAgentOptions` — 配置项（system_prompt, cwd, allowed_tools, max_turns, hooks, mcp_servers 等）

### 2.3 为什么不需要 Node.js 桥接

之前 Claude Code SDK 只有 Node.js 版本，Python 项目需要通过 HTTP 桥接调用。
现在 `claude-agent-sdk` 原生支持 Python，直接 `pip install` 即可：

```
之前：Python LangGraph → HTTP → Node.js Bridge → Claude Code SDK
现在：Python LangGraph → claude-agent-sdk（直接调用，零桥接）
```

---

## 3. 系统架构

### 3.1 整体架构图

```
                          ┌─────────────────────────────────────────┐
                          │            用户交互层                     │
                          │  ┌─────────────┐  ┌──────────────────┐  │
                          │  │  CLI 入口    │  │  Web / Flutter    │  │
                          │  │ (harness)    │  │  仪表盘           │  │
                          │  └──────┬───────┘  └────────┬─────────┘  │
                          └────────┼────────────────────┼────────────┘
                                   │                    │
                          ┌────────▼────────────────────▼────────────┐
                          │          FastAPI 服务层                    │
                          │  ┌───────────┐  ┌────────────────────┐   │
                          │  │  REST API  │  │  WebSocket 实时推送  │   │
                          │  └─────┬─────┘  └────────┬───────────┘   │
                          └───────┼──────────────────┼───────────────┘
                                  │                  │
                          ┌───────▼──────────────────▼───────────────┐
                          │          LangGraph 编排引擎                │
                          │                                           │
                          │  ┌─────────────────────────────────────┐  │
                          │  │          共享上下文管理器              │  │
                          │  │  ┌────────┐ ┌────────┐ ┌──────────┐ │  │
                          │  │  │ 全局   │ │ 领域   │ │ 任务     │ │  │
                          │  │  │ 上下文 │ │ 上下文 │ │ 上下文   │ │  │
                          │  │  └────────┘ └────────┘ └──────────┘ │  │
                          │  └─────────────────────────────────────┘  │
                          │                                           │
                          │  ┌──────────┐ ┌──────────┐ ┌──────────┐  │
                          │  │ 前端     │ │ 接口     │ │ 后端     │  │
                          │  │ Agent    │ │ Agent    │ │ Agent    │  │
                          │  └────┬─────┘ └────┬─────┘ └────┬─────┘  │
                          │       │            │            │         │
                          │  ┌────▼────────────▼────────────▼─────┐  │
                          │  │         Skills Agent                │  │
                          │  │    （经验沉淀 / .skills 生成）       │  │
                          │  └────────────────────────────────────┘  │
                          └──────────────┬───────────────────────────┘
                                         │
                          ┌──────────────▼───────────────────────────┐
                          │           执行层                          │
                          │                                           │
                          │  ┌──────────────┐  ┌──────────────────┐  │
                          │  │ claude-agent  │  │  Docker 沙箱     │  │
                          │  │ -sdk         │  │  代码验证环境     │  │
                          │  │              │  │                   │  │
                          │  │ • query()    │  │ • lint / build    │  │
                          │  │ • Client     │  │ • test            │  │
                          │  │ • Hooks      │  │ • 自定义检查      │  │
                          │  │ • MCP Tools  │  │                   │  │
                          │  └──────────────┘  └──────────────────┘  │
                          │                                           │
                          │  ┌──────────────────────────────────────┐ │
                          │  │           .skills 知识库              │ │
                          │  │   沉淀的经验文件，注入 Agent 上下文   │ │
                          │  └──────────────────────────────────────┘ │
                          └──────────────────────────────────────────┘
```

### 3.2 LangGraph 状态图

```
                    ┌──────────┐
                    │  START   │
                    └────┬─────┘
                         │
                         ▼
                ┌────────────────┐
                │  路由器 (Router)│ ← 分析用户意图，决定分发给哪个 Agent
                └───┬────┬────┬──┘
                    │    │    │
          ┌─────────┘    │    └─────────┐
          ▼              ▼              ▼
    ┌───────────┐  ┌───────────┐  ┌───────────┐
    │ 前端      │  │ 接口      │  │ 后端      │
    │ Agent     │  │ Agent     │  │ Agent     │
    └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
          │              │              │
          └──────┬───────┴──────┬───────┘
                 │              │
                 ▼              ▼
          ┌────────────┐  ┌──────────┐
          │ 沙箱验证    │  │ 直接应用  │
          │ (Docker)    │  │ (简单修改)│
          └──────┬──────┘  └─────┬────┘
                 │               │
           ┌─────┴─────┐        │
           ▼           ▼        │
     ┌──────────┐ ┌────────┐   │
     │ ✅ 通过   │ │ ❌ 失败 │   │
     │ 应用代码  │ │ 重写   │   │
     └────┬─────┘ └───┬────┘   │
          │           │        │
          │     ┌─────▼──────┐ │
          │     │ Skills     │ │
          │     │ Agent      │ │ ← 失败修复后触发经验沉淀
          │     │ 沉淀经验   │ │
          │     └─────┬──────┘ │
          │           │        │
          └───────┬───┴────────┘
                  ▼
            ┌──────────┐
            │   END    │
            └──────────┘
```

### 3.3 数据流

```
用户指令
    │
    ▼
CLI / Web / Flutter
    │
    ▼ (HTTP / WebSocket)
FastAPI
    │
    ▼
LangGraph Router
    │
    ├──→ 加载 .skills 相关经验
    ├──→ 加载共享上下文（全局 + 领域）
    │
    ▼
目标 Agent（ClaudeSDKClient）
    │
    ├──→ [PreToolUse Hook] 代码写入前审查
    │        │
    │        ├─ 通过 → 允许写入
    │        └─ 拒绝 → 返回原因，Agent 重试
    │
    ├──→ [自定义 MCP Tool] Docker 沙箱验证
    │        │
    │        ├─ 通过 → 代码应用到项目
    │        └─ 失败 → 错误反馈给 Agent → 重试
    │                    │
    │                    └─ 修复成功 → 触发 Skills Agent
    │                                     │
    │                                     └─ 生成 .skills 文件
    ▼
结果返回给用户（流式推送）
```

---

## 4. 核心模块设计

### 4.1 共享上下文管理器

**目的：** 解决多 Agent 终端间的上下文隔离问题

**分层设计：**

```python
class SharedContext:
    """三层上下文管理"""

    # Level 1: 全局上下文 —— 所有 Agent 共享
    # 包含：项目结构、技术栈、开发规范、Git 状态
    global_context: GlobalContext

    # Level 2: 领域上下文 —— 相关 Agent 共享
    # 前端：组件树、路由结构、样式系统
    # 后端：API 定义、DB Schema、业务模型
    # 接口：API 契约、数据格式、认证方式
    domain_contexts: dict[str, DomainContext]

    # Level 3: 任务上下文 —— 单个 Agent 私有
    # 当前任务描述、进行中的修改、临时状态
    task_contexts: dict[str, TaskContext]
```

**上下文注入方式：**

```python
# 通过 ClaudeAgentOptions 的 system_prompt 注入
options = ClaudeAgentOptions(
    system_prompt=f"""你是{agent_role}。

## 项目全局信息
{shared_context.global_context.to_prompt()}

## 领域上下文
{shared_context.get_domain(agent_domain).to_prompt()}

## 相关经验（.skills）
{skills_manager.get_relevant_skills(task_description)}

## 当前任务
{task_description}
""",
    cwd=project_dir,
)
```

**上下文更新机制：**

```python
# Agent 完成任务后，通过 LangGraph State 更新共享上下文
class HarnessState(TypedDict):
    messages: list[BaseMessage]
    shared_context: SharedContext
    current_agent: str
    task_queue: list[Task]
    validation_results: list[ValidationResult]
```

### 4.2 Agent 定义

#### 4.2.1 Base Agent

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

class BaseAgent:
    """所有 Agent 的基类，封装 ClaudeSDKClient"""

    def __init__(
        self,
        name: str,
        role: str,
        domain: str,
        shared_context: SharedContext,
        skills_manager: SkillsManager,
        sandbox: DockerSandbox,
    ):
        self.name = name
        self.role = role
        self.domain = domain
        self.shared_context = shared_context
        self.skills_manager = skills_manager
        self.sandbox = sandbox

    def _build_options(self, task: str) -> ClaudeAgentOptions:
        """构建 Agent 配置"""
        relevant_skills = self.skills_manager.get_relevant(task)

        return ClaudeAgentOptions(
            system_prompt=self._build_system_prompt(task, relevant_skills),
            cwd=str(self.shared_context.project_dir),
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            max_turns=15,
            hooks=self._build_hooks(),
            mcp_servers=self._build_mcp_servers(),
        )

    def _build_hooks(self) -> dict:
        """构建 Hooks — 代码审查拦截"""
        return {
            "PreToolUse": [
                HookMatcher(
                    matcher="Write",
                    hooks=[self._review_before_write]
                ),
                HookMatcher(
                    matcher="Edit",
                    hooks=[self._review_before_edit]
                ),
            ]
        }

    def _build_mcp_servers(self) -> dict:
        """构建自定义 MCP 工具 — Docker 沙箱"""
        sandbox_tool = create_sdk_mcp_server(
            name="sandbox",
            version="1.0.0",
            tools=[self.sandbox.verify_tool]
        )
        return {"sandbox": sandbox_tool}

    async def execute(self, task: str) -> AgentResult:
        """执行任务"""
        options = self._build_options(task)

        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            messages = []
            async for msg in client.receive_response():
                messages.append(msg)
                yield msg  # 流式输出

        # 更新共享上下文
        self.shared_context.update_domain(
            self.domain,
            self._extract_changes(messages)
        )

        return AgentResult(agent=self.name, messages=messages)
```

#### 4.2.2 具体 Agent

```python
class FrontendAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(
            name="frontend",
            role="前端 UI 开发专家",
            domain="frontend",
            **kwargs,
        )

class APIAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(
            name="api",
            role="前后端接口联调专家",
            domain="api",
            **kwargs,
        )

class BackendAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(
            name="backend",
            role="后端业务开发专家",
            domain="backend",
            **kwargs,
        )

class SkillsAgent(BaseAgent):
    """经验沉淀 Agent — 分析错误修复过程，生成 .skills 文件"""
    def __init__(self, **kwargs):
        super().__init__(
            name="skills",
            role="经验分析与沉淀专家",
            domain="skills",
            **kwargs,
        )

    async def analyze_and_save(
        self, error_log: str, fix_log: str, context: str
    ) -> str:
        """分析错误 → 修复过程，生成 .skills 文件"""
        prompt = f"""分析以下错误和修复过程，生成一份结构化的经验文件：

## 错误信息
{error_log}

## 修复过程
{fix_log}

## 上下文
{context}

请按以下格式输出：
---
title: [简短标题]
tags: [相关标签]
severity: [high/medium/low]
---

## 问题描述
[描述问题]

## 根本原因
[分析根因]

## 解决方案
[修复方法]

## 预防措施
[如何避免再次发生]
"""
        result = await self.execute(prompt)
        return result
```

### 4.3 Docker 沙箱验证

```python
import docker
from claude_agent_sdk import tool

class DockerSandbox:
    """Docker 沙箱管理器"""

    def __init__(self, project_dir: str, config: SandboxConfig):
        self.client = docker.from_env()
        self.project_dir = project_dir
        self.config = config

    @tool("verify_code", "在 Docker 沙箱中验证代码", {
        "test_type": str,  # "lint" | "build" | "test" | "custom"
        "target": str,     # 验证目标路径或命令
    })
    async def verify_tool(self, args: dict) -> dict:
        """作为 MCP Tool 暴露给 Agent"""
        test_type = args["test_type"]
        target = args["target"]

        result = await self.run_verification(test_type, target)

        return {
            "content": [{
                "type": "text",
                "text": f"验证结果: {'✅ 通过' if result.success else '❌ 失败'}\n"
                        f"输出:\n{result.output}\n"
                        f"错误:\n{result.errors}"
            }]
        }

    async def run_verification(
        self, test_type: str, target: str
    ) -> VerificationResult:
        """在 Docker 容器中运行验证"""
        commands = {
            "lint": f"cd /workspace && npx eslint {target} || python -m flake8 {target}",
            "build": f"cd /workspace && npm run build || python -m build",
            "test": f"cd /workspace && npm test || python -m pytest {target}",
            "custom": target,
        }

        container = self.client.containers.run(
            image=self.config.image,
            command=["sh", "-c", commands[test_type]],
            volumes={
                self.project_dir: {"bind": "/workspace", "mode": "ro"}
            },
            detach=True,
            mem_limit="512m",
            cpu_period=100000,
            cpu_quota=50000,  # 50% CPU
            network_mode="none",  # 禁止网络访问
        )

        result = container.wait(timeout=self.config.timeout)
        logs = container.logs().decode()
        container.remove()

        return VerificationResult(
            success=result["StatusCode"] == 0,
            output=logs,
            errors=logs if result["StatusCode"] != 0 else "",
        )
```

### 4.4 Hooks — 代码审查

```python
from claude_agent_sdk import HookMatcher

async def review_before_write(input_data, tool_use_id, context):
    """
    PreToolUse Hook: 在每次文件写入前自动审查
    解决用户"直接 yes 不 review"的问题
    """
    tool_name = input_data["tool_name"]
    if tool_name not in ("Write", "Edit"):
        return {}

    file_path = input_data["tool_input"].get("file_path", "")
    content = input_data["tool_input"].get("content", "")

    # 检查 1: 危险文件保护
    protected_files = [".env", "credentials", "secret", "password"]
    for pattern in protected_files:
        if pattern in file_path.lower():
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"保护文件 {file_path} 不可被自动修改",
                }
            }

    # 检查 2: 文件大小限制（防止覆盖大文件）
    if len(content) > 10000:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason":
                    f"文件 {file_path} 修改量较大（{len(content)} 字符），请确认",
            }
        }

    # 检查 3: 沙箱快速验证（语法检查）
    if file_path.endswith((".py", ".js", ".ts", ".tsx")):
        syntax_ok = await quick_syntax_check(file_path, content)
        if not syntax_ok:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "代码语法检查未通过",
                }
            }

    return {}  # 允许写入


async def capture_error_for_skills(input_data, tool_use_id, context):
    """
    PostToolUse Hook: 捕获工具执行错误，用于后续经验沉淀
    """
    result = input_data.get("tool_result", {})
    if result.get("is_error", False):
        error_info = {
            "tool_name": input_data["tool_name"],
            "tool_input": input_data["tool_input"],
            "error": result.get("content", ""),
            "timestamp": datetime.now().isoformat(),
        }
        # 记录到错误队列，后续由 Skills Agent 处理
        await error_queue.put(error_info)

    return {}
```

### 4.5 经验沉淀系统

```python
class SkillsManager:
    """经验知识库管理"""

    def __init__(self, skills_dir: str):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._index: list[SkillEntry] = []
        self._load_index()

    def _load_index(self):
        """加载所有 .skills 文件并建立索引"""
        for skill_file in self.skills_dir.glob("*.md"):
            entry = self._parse_skill_file(skill_file)
            self._index.append(entry)

    def get_relevant(self, task: str, top_k: int = 5) -> list[SkillEntry]:
        """根据任务描述检索相关经验"""
        # 简单实现：基于关键词匹配
        # 后续可升级为向量检索
        scored = []
        for entry in self._index:
            score = self._relevance_score(task, entry)
            if score > 0:
                scored.append((score, entry))
        scored.sort(reverse=True, key=lambda x: x[0])
        return [entry for _, entry in scored[:top_k]]

    async def save_skill(
        self,
        title: str,
        content: str,
        tags: list[str],
        severity: str,
    ):
        """保存新的经验文件"""
        filename = self._generate_filename(title)
        filepath = self.skills_dir / filename

        frontmatter = f"""---
title: {title}
tags: {tags}
severity: {severity}
created: {datetime.now().isoformat()}
---

{content}
"""
        filepath.write_text(frontmatter, encoding="utf-8")
        self._load_index()  # 刷新索引

    def _should_trigger(self, retry_count: int) -> bool:
        """判断是否触发经验沉淀（重试 >= 2 次才触发）"""
        return retry_count >= 2
```

### 4.6 LangGraph 编排

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

class HarnessOrchestrator:
    """LangGraph 编排引擎"""

    def __init__(self, config: HarnessConfig):
        self.config = config
        self.shared_context = SharedContext(config.project_dir)
        self.skills_manager = SkillsManager(config.skills_dir)
        self.sandbox = DockerSandbox(config.project_dir, config.sandbox)

        # 初始化各 Agent
        agent_kwargs = dict(
            shared_context=self.shared_context,
            skills_manager=self.skills_manager,
            sandbox=self.sandbox,
        )
        self.agents = {
            "frontend": FrontendAgent(**agent_kwargs),
            "api": APIAgent(**agent_kwargs),
            "backend": BackendAgent(**agent_kwargs),
            "skills": SkillsAgent(**agent_kwargs),
        }

        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 状态图"""
        graph = StateGraph(HarnessState)

        # 添加节点
        graph.add_node("router", self._route_task)
        graph.add_node("frontend_agent", self._run_frontend)
        graph.add_node("api_agent", self._run_api)
        graph.add_node("backend_agent", self._run_backend)
        graph.add_node("sandbox_verify", self._verify_in_sandbox)
        graph.add_node("apply_code", self._apply_code)
        graph.add_node("skills_agent", self._run_skills)

        # 设置入口
        graph.set_entry_point("router")

        # 路由分发
        graph.add_conditional_edges(
            "router",
            self._determine_agent,
            {
                "frontend": "frontend_agent",
                "api": "api_agent",
                "backend": "backend_agent",
            }
        )

        # Agent 完成后 → 沙箱验证
        for agent_node in ["frontend_agent", "api_agent", "backend_agent"]:
            graph.add_conditional_edges(
                agent_node,
                self._needs_sandbox,
                {
                    "verify": "sandbox_verify",
                    "direct": "apply_code",
                }
            )

        # 沙箱结果判断
        graph.add_conditional_edges(
            "sandbox_verify",
            self._sandbox_result,
            {
                "pass": "apply_code",
                "fail": "router",  # 重试，回到路由
            }
        )

        # 应用代码后判断是否需要沉淀经验
        graph.add_conditional_edges(
            "apply_code",
            self._needs_skill_capture,
            {
                "capture": "skills_agent",
                "done": END,
            }
        )

        graph.add_edge("skills_agent", END)

        return graph.compile(checkpointer=MemorySaver())

    async def _route_task(self, state: HarnessState) -> HarnessState:
        """路由器：分析任务意图，决定分发给哪个 Agent"""
        task = state["messages"][-1].content

        # 使用 claude-agent-sdk 快速分析意图
        intent = await self._analyze_intent(task)
        state["current_agent"] = intent
        return state

    def _determine_agent(self, state: HarnessState) -> str:
        """根据分析结果返回目标 Agent"""
        return state["current_agent"]
```

### 4.7 FastAPI 服务层

```python
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Harness Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/task")
async def submit_task(task: TaskRequest):
    """提交新任务"""
    result = await orchestrator.run(task.prompt)
    return {"task_id": result.id, "status": "submitted"}

@app.get("/api/agents")
async def list_agents():
    """获取所有 Agent 及其状态"""
    return orchestrator.get_agent_status()

@app.get("/api/skills")
async def list_skills():
    """获取已沉淀的 Skills 列表"""
    return skills_manager.list_all()

@app.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str):
    """获取单个 Skill 详情"""
    return skills_manager.get(skill_id)

@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: str, data: SkillUpdate):
    """编辑 Skill（人工确认/修改）"""
    return skills_manager.update(skill_id, data)

@app.websocket("/ws/task/{task_id}")
async def task_stream(websocket: WebSocket, task_id: str):
    """WebSocket 实时推送任务状态和 Agent 输出"""
    await websocket.accept()
    async for event in orchestrator.stream(task_id):
        await websocket.send_json({
            "type": event.type,  # "agent_output" | "status" | "error"
            "agent": event.agent,
            "data": event.data,
            "timestamp": event.timestamp,
        })
```

### 4.8 CLI 入口

```python
import click

@click.group()
def cli():
    """Harness Agent — 智能开发编排系统"""
    pass

@cli.command()
@click.option("--project", "-p", default=".", help="项目目录")
@click.option("--port", default=8000, help="API 端口")
def start(project: str, port: int):
    """启动 Harness Agent"""
    click.echo(f"🚀 Harness Agent 启动中...")
    click.echo(f"📁 项目: {project}")
    click.echo(f"🌐 API: http://localhost:{port}")
    click.echo(f"📊 仪表盘: http://localhost:{port}/dashboard")

    config = HarnessConfig(project_dir=project, port=port)
    orchestrator = HarnessOrchestrator(config)
    start_server(orchestrator, port)

@cli.command()
@click.argument("prompt")
@click.option("--agent", "-a", help="指定 Agent（frontend/api/backend）")
def run(prompt: str, agent: str = None):
    """直接运行一个任务"""
    asyncio.run(orchestrator.run(prompt, agent=agent))

@cli.command()
def skills():
    """查看已沉淀的 Skills"""
    for skill in skills_manager.list_all():
        click.echo(f"  [{skill.severity}] {skill.title} ({skill.tags})")

if __name__ == "__main__":
    cli()
```

---

## 5. 项目结构

```
harness-agent/
│
├── core/                           # 核心引擎
│   ├── __init__.py
│   ├── orchestrator.py             # LangGraph 编排引擎
│   ├── context.py                  # 共享上下文管理器
│   ├── config.py                   # 配置定义
│   └── agents/                     # Agent 定义
│       ├── __init__.py
│       ├── base_agent.py           # Agent 基类（封装 ClaudeSDKClient）
│       ├── frontend_agent.py       # 前端 UI Agent
│       ├── api_agent.py            # 接口联调 Agent
│       ├── backend_agent.py        # 后端业务 Agent
│       └── skills_agent.py         # 经验沉淀 Agent
│
├── tools/                          # 自定义 MCP 工具
│   ├── __init__.py
│   ├── sandbox_tool.py             # Docker 沙箱验证工具
│   ├── test_runner.py              # 测试运行器
│   └── skills_writer.py            # Skills 文件写入工具
│
├── hooks/                          # Hooks（行为拦截）
│   ├── __init__.py
│   ├── code_review_hook.py         # 代码写入前审查
│   ├── error_capture_hook.py       # 错误捕获 → 触发经验沉淀
│   └── safety_hook.py              # 安全保护（敏感文件等）
│
├── skills/                         # 沉淀的经验文件
│   ├── __init__.py
│   ├── manager.py                  # Skills 管理器
│   └── entries/                    # .skills 文件存放目录
│       └── *.md
│
├── server/                         # FastAPI 后端
│   ├── __init__.py
│   ├── main.py                     # FastAPI 入口
│   ├── routes/
│   │   ├── tasks.py                # 任务相关 API
│   │   ├── agents.py               # Agent 状态 API
│   │   └── skills.py               # Skills 管理 API
│   └── websocket.py                # WebSocket 实时推送
│
├── web/                            # React + TypeScript 前端（Web 仪表盘）
│   ├── src/
│   │   ├── components/
│   │   │   ├── AgentGraph.tsx       # Agent 拓扑图可视化
│   │   │   ├── AgentStatus.tsx      # Agent 状态卡片
│   │   │   ├── TaskStream.tsx       # 任务流式输出
│   │   │   ├── SkillsList.tsx       # Skills 列表
│   │   │   └── SkillEditor.tsx      # Skills 编辑器
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx        # 仪表盘主页
│   │   │   ├── Tasks.tsx            # 任务管理
│   │   │   └── Skills.tsx           # Skills 浏览
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── package.json
│   └── tsconfig.json
│
├── app/                            # Flutter 前端（跨端客户端）
│   ├── lib/
│   │   ├── main.dart
│   │   ├── screens/
│   │   ├── widgets/
│   │   └── services/
│   └── pubspec.yaml
│
├── docker/                         # Docker 相关
│   ├── Dockerfile.sandbox          # 沙箱镜像
│   ├── Dockerfile.app              # 应用部署镜像
│   └── docker-compose.yml
│
├── tests/                          # 测试
│   ├── test_agents.py
│   ├── test_orchestrator.py
│   ├── test_sandbox.py
│   └── test_hooks.py
│
├── docs/                           # 文档
│   └── sdk_bridge_solutions.md     # SDK 方案记录
│
├── cli.py                          # CLI 入口
├── pyproject.toml                  # Python 项目配置
├── README.md
└── .env.example                    # 环境变量模板
```

---

## 6. SDK 能力与需求映射

| 用户需求 | SDK 能力 | 实现方式 |
|---------|---------|---------|
| 多终端共享上下文 | `ClaudeSDKClient` + Session | LangGraph State 管理共享上下文，注入 `system_prompt` |
| 自动代码审查 | `Hooks (PreToolUse)` | Write/Edit 操作前自动拦截，语法检查 + 安全检查 |
| Docker 沙箱验证 | `@tool` + MCP Server | 自定义 `verify_code` 工具，Agent 主动调用 |
| 经验沉淀到 .skills | `SkillsAgent` + `PostToolUse` Hook | 错误捕获 → Skills Agent 分析 → 生成 .md 文件 |
| 流式输出 | `AsyncIterator` | Agent 输出实时推送到 WebSocket → 前端 |
| 独立 CLI | `click` | `harness start` / `harness run` / `harness skills` |
| 可视化仪表盘 | FastAPI + React/Flutter | Agent 拓扑、状态监控、Skills 浏览编辑 |

---

## 7. 开发阶段规划

### Phase 1：最小可用核心（2-3 周）

**目标：** 跑通 claude-agent-sdk 调用 + LangGraph 最简编排

- [ ] 项目骨架搭建（pyproject.toml, 目录结构）
- [ ] 安装 `claude-agent-sdk`，验证 `query()` 和 `ClaudeSDKClient` 可用
- [ ] 实现 `BaseAgent` 封装 `ClaudeSDKClient`
- [ ] 实现最简 LangGraph 图（Router → 单 Agent → 输出）
- [ ] CLI 入口（`harness run "创建一个 hello.py"`）

**交付物：** 能通过 CLI 发送指令，Agent 完成代码生成

### Phase 2：多 Agent + 共享上下文（2-3 周）

**目标：** 多 Agent 协作，上下文共享

- [ ] 实现 `SharedContext` 三层上下文管理器
- [ ] 实现 Frontend / API / Backend 三个专业 Agent
- [ ] LangGraph Router 根据意图自动分发
- [ ] Agent 间通过 State 传递上下文
- [ ] 支持用户指定 Agent（`harness run --agent frontend "..."`)

**交付物：** 多 Agent 协作，各自了解项目全貌

### Phase 3：代码质量保障（2 周）

**目标：** Hooks 审查 + Docker 沙箱验证

- [ ] 实现 `PreToolUse` Hook（写入前语法检查、安全保护）
- [ ] 实现 Docker 沙箱管理器
- [ ] 实现 `verify_code` MCP Tool
- [ ] 验证流程：生成 → Hook 审查 → 沙箱验证 → 应用/重试

**交付物：** 代码自动审查 + 沙箱验证，质量有保障

### Phase 4：经验沉淀（1-2 周）

**目标：** 自动生成 .skills 文件

- [ ] 实现 `PostToolUse` Hook 捕获错误
- [ ] 实现 `SkillsAgent` 分析错误 → 生成 .skills
- [ ] 实现 `SkillsManager` 加载/检索/注入
- [ ] 沉淀触发条件：重试次数 ≥ 2
- [ ] Skills 加载到 Agent 的 `system_prompt`

**交付物：** 踩坑自动记录，经验越积越多

### Phase 5：监控前端（2-3 周）

**目标：** Web + Flutter 仪表盘

- [ ] FastAPI 后端 + WebSocket
- [ ] React Web 仪表盘：Agent 拓扑图、状态监控、任务流
- [ ] Skills 浏览 / 编辑界面
- [ ] Flutter 跨端客户端（基础版）

**交付物：** 可视化监控 + 管理界面

### Phase 6：打磨与优化（持续）

- [ ] 上下文检索升级（向量检索）
- [ ] Skills 质量评估机制
- [ ] Agent 自适应策略（根据历史表现调整参数）
- [ ] 性能优化（并行 Agent、缓存）
- [ ] 完善文档和测试

---

## 8. 环境依赖

### 8.1 pyproject.toml 核心依赖

```toml
[project]
name = "harness-agent"
version = "0.1.0"
requires-python = ">=3.10"

dependencies = [
    "claude-agent-sdk>=0.2.90",
    "langgraph>=0.2.0",
    "langchain-core>=0.3.0",
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "websockets>=12.0",
    "docker>=7.0.0",
    "click>=8.1.0",
    "anyio>=4.0.0",
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.5.0",
]

[project.scripts]
harness = "cli:cli"
```

### 8.2 系统要求

- Python ≥ 3.10
- Docker（用于沙箱验证）
- Node.js（用于 React Web 前端构建）
- Flutter SDK（用于跨端客户端，Phase 5）

---

## 9. 风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|---------|
| claude-agent-sdk API 变更 | 代码需要适配 | 封装 BaseAgent 抽象层，降低耦合 |
| Docker 沙箱性能开销 | 验证速度慢 | 容器池预热 + 快速语法检查走 Hook |
| Skills 质量不高 | 垃圾经验干扰 Agent | 设置触发阈值 + 用户确认后入库 |
| 上下文 token 超限 | Agent 丢失信息 | 分层上下文 + 摘要压缩 |
| LangGraph 调试困难 | 开发效率低 | 利用 LangSmith 可视化调试 |

---

## 10. 总结

| 维度 | 评价 |
|------|------|
| 痛点定位 | ✅ 精准：多终端隔离、缺少审查、经验无沉淀 |
| 技术选型 | ✅ 纯 Python：claude-agent-sdk + LangGraph + FastAPI |
| 架构设计 | ✅ 清晰分层：编排 → Agent → 执行（SDK + 沙箱 + Skills）|
| SDK 适配 | ✅ 完美匹配：Hooks 做审查、MCP Tools 做沙箱、Session 做上下文 |
| 开发节奏 | ✅ 6 阶段渐进交付，每阶段有可运行产物 |
| 技术风险 | ⚠️ 中等：主要在沙箱性能和 SDK 稳定性 |
