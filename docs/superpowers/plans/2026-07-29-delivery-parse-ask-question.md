# 交付轨解析反问可视化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付轨 FIFO 解析需求时，解析 query 中模型调用 `AskUserQuestion` 可被拦截、问题卡渲染进右侧交付轨抽屉、用户经共享输入框作答、答案回注后解析继续。

**Architecture:** 复用交互轨反问机制（`can_use_tool` 权限回调 + Future + answer 模式），渲染目标参数化：交互轨→`ContentBuffer`，交付轨→`DrawerPanel`。解析 query（`architect.py:_parse_requirement`）从 `disallowed_tools` 移除 `AskUserQuestion`，经 `ClaudeAgentOptions.can_use_tool` 接 chat 层注入的 `_delivery_can_use_tool` 回调。双轨竞态：后到提问 await 已有 pending Future 了结再占槽。

**Tech Stack:** Python 3.11+, claude-agent-sdk, prompt_toolkit, pytest (asyncio mode auto)。

**Spec:** `docs/superpowers/specs/2026-07-29-delivery-parse-ask-question-design.md`

## Global Constraints

- 解析阶段只读语义不破：`_delivery_can_use_tool` 对非 `AskUserQuestion` 一律 `PermissionResultDeny`。
- `allowed_tools` **不加** `AskUserQuestion`（预放行则回调不触发）。
- 无头调用（未注入 `parse_can_use_tool`）用 `_default_can_use_tool`，行为同现状（拒绝反问）。
- 现有 `tests/test_ask_user_question.py` 全绿为回归基线，不得改其断言。
- 测试不拉真实 SDK/CLI：`session_store` 用 `MagicMock(spec=SessionStore)`，`ChatCLI` 不 `start()`。
- 提交信息中文、conventional commits 前缀（feat/refactor/test）。

---

### Task 1: DrawerPanel.append_question_card

**Files:**
- Modify: `src/autoloop_agent/chat/drawer.py:61-64`（`append_log` 后新增方法）
- Test: `tests/test_drawer.py`

**Interfaces:**
- Produces: `DrawerPanel.append_question_card(question: str, options: list[str]) -> None` — 问题卡多行进 `log_buf` 流水（Task 3 作 `render_card` 回调消费）。

- [ ] **Step 1: Write the failing test**

`tests/test_drawer.py` 追加：

```python
def test_append_question_card_into_log():
    """解析反问卡：头部/问题/编号选项/footer 全进抽屉流水。"""
    from autoloop_agent.chat.drawer import DrawerPanel
    drawer = DrawerPanel()
    drawer.append_question_card("用哪个方案？", ["方案A — 快", "方案B — 稳"])
    text = "".join(frag[1] for frag in drawer.log_buf.get_formatted_text())
    assert "┌ 解析反问" in text
    assert "用哪个方案？" in text
    assert "1. 方案A — 快" in text
    assert "2. 方案B — 稳" in text
    assert "└" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drawer.py::test_append_question_card_into_log -v`
Expected: FAIL `AttributeError: 'DrawerPanel' object has no attribute 'append_question_card'`

- [ ] **Step 3: Write minimal implementation**

`src/autoloop_agent/chat/drawer.py` `append_log` 之后加：

```python
    def append_question_card(self, question: str, options: list[str]) -> None:
        """解析反问卡(进流水):边框样式对齐 ContentBuffer.append_question_card。"""
        self.append_log("┌ 解析反问 ──────────────────")
        self.append_log(f"│ {question}")
        for i, opt in enumerate(options, 1):
            self.append_log(f"│  {i}. {opt}")
        self.append_log("└ 输入编号选择，或直接输入自定义答案")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_drawer.py -v`
Expected: PASS（新测试 + 既有 drawer 测试全绿）

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/chat/drawer.py tests/test_drawer.py
git commit -m "feat: DrawerPanel.append_question_card 解析反问卡入抽屉流水"
```

---

### Task 2: repl 抽 _ask_questions 公共方法（重构，回归守卫）

**Files:**
- Modify: `src/autoloop_agent/chat/repl.py:110-182`（`_can_use_tool` / `_resolve_answer` / `__init__` 状态区）

**Interfaces:**
- Produces:
  - `ChatCLI._ask_questions(tool_input: dict, *, render_card: Callable[[str, list[str]], None], echo: Callable[[str], None]) -> PermissionResult`（Task 3 消费）
  - `ChatCLI._pending_echo: Callable[[str], None] | None`（实例字段，answer 回显目标）
- Consumes: 行为须与重构前完全一致——`tests/test_ask_user_question.py` 不改断言全绿。

- [ ] **Step 1: 重构 `__init__` 状态区（repl.py:110-113 附近）追加 echo 字段**

```python
        # ── AskUserQuestion 待回答状态 ──
        # 非 None 时用户输入路由给该 Future（answer 模式），而非发新消息。
        self._pending_answer: asyncio.Future | None = None
        self._pending_options: list[str] = []  # 当前问题选项（数字映射用）
        self._pending_echo: Callable[[str], None] | None = None  # 回答回显目标(交互轨=主区,交付轨=抽屉)
```

文件头 import 区确认有 `from collections.abc import Callable`（无则加）。

- [ ] **Step 2: 抽 `_ask_questions`，`_can_use_tool` 改薄壳**

替换 repl.py 现 `_can_use_tool`（131-168 行）整体为：

```python
    async def _can_use_tool(
        self, tool_name: str, tool_input: dict, context: ToolPermissionContext
    ) -> PermissionResult:
        """常驻执行体权限回调：非 AskUserQuestion 直接放行；
        AskUserQuestion 走公共反问流程（渲染目标=主区 content_buffer）。"""
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow()
        return await self._ask_questions(
            tool_input,
            render_card=self.content_buffer.append_question_card,
            echo=self.content_buffer.append_plain,
        )

    async def _ask_questions(
        self,
        tool_input: dict,
        *,
        render_card: Callable[[str, list[str]], None],
        echo: Callable[[str], None],
    ) -> PermissionResult:
        """AskUserQuestion 公共流程(双轨共用):渲染卡 -> 排队占槽 -> 逐问等答 -> 回注 answers。

        竞态:已有 pending answer(另一轨提问中)时先等其了结再占槽(后到排队)。
        render_card/echo 由调用方定渲染目标:交互轨=content_buffer,交付轨=drawer。
        """
        prev = self._pending_answer
        if prev is not None and not prev.done():
            await asyncio.wait([prev])  # 不传播 prev 的取消/异常;自身取消照常抛出
        answers: dict[str, str] = {}
        for q in tool_input.get("questions", []):
            question_text = q.get("question", "")
            labels = [o.get("label", "") for o in q.get("options", [])]
            display = [
                f"{o.get('label', '')} — {o.get('description')}"
                if o.get("description") else o.get("label", "")
                for o in q.get("options", [])
            ]
            render_card(question_text, display)
            self.tui.invalidate()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_answer = fut
            # 数字映射用纯 label（answers 值）；display 仅供卡片展示
            self._pending_options = labels
            self._pending_echo = echo
            self.tui.set_answer_mode(True)
            try:
                answer = await fut
            except asyncio.CancelledError:
                # ESC 取消整个消息：Future 被取消，拒绝本次提问（不悬挂）
                return PermissionResultDeny(message="用户取消了提问")
            finally:
                self._pending_answer = None
                self._pending_options = []
                self._pending_echo = None
                self.tui.set_answer_mode(False)
            answers[question_text] = answer
        return PermissionResultAllow(
            updated_input={**tool_input, "answers": answers}
        )
```

- [ ] **Step 3: `_resolve_answer` 回显走 `_pending_echo`**

`_resolve_answer` 中 `self.content_buffer.append_plain(f"回答> {answer}")` 改为：

```python
        echo = self._pending_echo or self.content_buffer.append_plain
        echo(f"回答> {answer}")
```

- [ ] **Step 4: Run regression tests**

Run: `pytest tests/test_ask_user_question.py -v`
Expected: 全绿（行为零变化；这是本任务唯一验收）

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/chat/repl.py
git commit -m "refactor: 抽 _ask_questions 公共反问流程,渲染目标参数化"
```

---

### Task 3: repl._delivery_can_use_tool（交付轨反问回调）

**Files:**
- Modify: `src/autoloop_agent/chat/repl.py`（`_can_use_tool` 后新增方法）
- Test: 新建 `tests/test_delivery_ask_question.py`

**Interfaces:**
- Consumes: Task 1 `DrawerPanel.append_question_card`；Task 2 `_ask_questions` / `_pending_echo`。
- Produces: `ChatCLI._delivery_can_use_tool(tool_name: str, tool_input: dict, context) -> PermissionResult`（Task 5 注入 Governor）。

- [ ] **Step 1: Write the failing tests**

新建 `tests/test_delivery_ask_question.py`：

```python
"""交付轨解析反问(_delivery_can_use_tool) 单元测试
覆盖：
- 非 AskUserQuestion 工具一律 PermissionResultDeny（保解析只读语义）
- AskUserQuestion 问题卡进 drawer 流水，不进主区 content_buffer
- answer 模式数字/文字映射 answers，回显进 drawer
- 双轨竞态：交互轨 pending 时交付轨提问排队，前答完才占槽渲卡
- ESC 取消：Deny 不悬挂，answer 模式复位
不拉真实 SDK/CLI：session_store 用 MagicMock；ChatCLI 不 start()。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    SessionStore,
)

from autoloop_agent.chat.content_buffer import ContentBuffer
from autoloop_agent.chat.repl import ChatCLI


def make_cli() -> ChatCLI:
    return ChatCLI(project_dir=".", session_store=MagicMock(spec=SessionStore))


def make_question_input() -> dict:
    return {
        "questions": [
            {
                "question": "用哪个方案？",
                "header": "方案",
                "options": [
                    {"label": "方案A", "description": "快"},
                    {"label": "方案B", "description": "稳"},
                ],
                "multiSelect": False,
            },
            {"question": "叫什么名字？", "header": "名", "options": [], "multiSelect": False},
        ]
    }


def buf_text(buf: ContentBuffer) -> str:
    return "".join(frag[1] for frag in buf.get_formatted_text())


async def test_non_ask_tool_denied():
    """解析阶段非 AskUserQuestion 工具一律拒绝（只读语义由 allowed_tools 白名单保证，
    白名单外的工具到回调这里全拒）。"""
    cli = make_cli()
    for tool_name in ("Bash", "Write", "Edit"):
        result = await cli._delivery_can_use_tool(tool_name, {}, None)
        assert isinstance(result, PermissionResultDeny)


async def test_ask_card_into_drawer_not_main_buffer():
    """AskUserQuestion 卡片进 drawer 流水，主区 content_buffer 无卡片。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    drawer_text = buf_text(cli._drawer.log_buf)
    assert "┌ 解析反问" in drawer_text
    assert "用哪个方案？" in drawer_text
    assert "1. 方案A — 快" in drawer_text
    assert "用哪个方案？" not in buf_text(cli.content_buffer)
    cli._cancel_current_request()
    await task


async def test_answer_digit_maps_and_echo_into_drawer():
    """数字映射选项 label，answers 回注，回显进 drawer。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    await cli._on_user_input("2")
    await asyncio.sleep(0.05)  # 进入第 2 问
    await cli._on_user_input("自定义名")
    result = await task
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {
        "用哪个方案？": "方案B",
        "叫什么名字？": "自定义名",
    }
    assert "回答> 方案B" in buf_text(cli._drawer.log_buf)


async def test_delivery_ask_queues_behind_interactive():
    """双轨竞态：交互轨提问占用 answer 槽时，交付轨提问排队；
    交互轨答完后交付轨才渲卡占槽。"""
    cli = make_cli()
    t1 = asyncio.create_task(
        cli._can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    # 交付轨排队中：卡片未进 drawer
    assert "┌ 解析反问" not in buf_text(cli._drawer.log_buf)
    # 答完交互轨两问
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    await cli._on_user_input("x")
    await t1
    await asyncio.sleep(0.05)
    # 交付轨占槽：卡片进 drawer
    assert "┌ 解析反问" in buf_text(cli._drawer.log_buf)
    await cli._on_user_input("1")
    await asyncio.sleep(0.05)
    await cli._on_user_input("y")
    result2 = await t2
    assert result2.updated_input["answers"] == {
        "用哪个方案？": "方案A",
        "叫什么名字？": "y",
    }


async def test_esc_cancel_deny_not_dangling():
    """answer 模式 ESC：Deny 返回不悬挂，槽位/answer 模式复位。"""
    cli = make_cli()
    task = asyncio.create_task(
        cli._delivery_can_use_tool("AskUserQuestion", make_question_input(), None)
    )
    await asyncio.sleep(0.05)
    assert cli._pending_answer is not None
    cli._cancel_current_request()
    result = await asyncio.wait_for(task, timeout=1)
    assert isinstance(result, PermissionResultDeny)
    assert cli._pending_answer is None
    assert not cli.tui.answer_mode
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_delivery_ask_question.py -v`
Expected: FAIL `AttributeError: 'ChatCLI' object has no attribute '_delivery_can_use_tool'`

- [ ] **Step 3: Write minimal implementation**

repl.py `_can_use_tool`（薄壳版）之后加：

```python
    async def _delivery_can_use_tool(
        self, tool_name: str, tool_input: dict, context: ToolPermissionContext
    ) -> PermissionResult:
        """交付轨解析 query 权限回调：非 AskUserQuestion 一律拒绝（保解析只读）；
        AskUserQuestion 走公共反问流程（渲染目标=右侧抽屉）。"""
        if tool_name != "AskUserQuestion":
            return PermissionResultDeny(message="解析阶段仅允许只读工具与反问")
        return await self._ask_questions(
            tool_input,
            render_card=self._drawer.append_question_card,
            echo=self._drawer.append_log,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_delivery_ask_question.py tests/test_ask_user_question.py -v`
Expected: 两文件全绿

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/chat/repl.py tests/test_delivery_ask_question.py
git commit -m "feat: repl._delivery_can_use_tool 交付轨反问回调,卡片入抽屉+排队竞态"
```

---

### Task 4: architect 接线（Governor.parse_can_use_tool + _parse_requirement）

**Files:**
- Modify: `src/autoloop_agent/core/architect.py:155-162`（REQUIREMENT_PARSER_PROMPT）、`313-338`（`__init__`）、`439-463`（`_parse_requirement` opts）
- Test: `tests/test_governor.py`（追加；无则新建）

**Interfaces:**
- Consumes: 无（architect 层独立）。
- Produces:
  - `Governor(project_dir, model, *, session_store, can_use_tool, parse_can_use_tool)` — 新构造参数 `parse_can_use_tool: CanUseTool | None = None`，存 `self._parse_can_use_tool`（缺省 `_default_can_use_tool`），rebuild 不丢（Task 5 注入）。
  - `_parse_requirement` 的 `ClaudeAgentOptions` 带 `can_use_tool=self._parse_can_use_tool`，`disallowed_tools` 无 `AskUserQuestion`。

- [ ] **Step 1: Write the failing tests**

`tests/test_governor.py` 追加（文件头补 `from unittest.mock import AsyncMock, MagicMock`、`import autoloop_agent.core.architect as architect_module`、`from claude_agent_sdk import SessionStore`、`import pytest`，按文件现状补齐缺的）：

```python
async def test_parse_query_wires_can_use_tool_and_allows_ask(monkeypatch):
    """_parse_requirement 的 ClaudeAgentOptions：can_use_tool 挂载注入回调，
    disallowed_tools 不含 AskUserQuestion（走回调拦截而非禁掉）。"""
    seen = {}

    async def fake_query(*, prompt, options):
        seen["options"] = options
        return
        yield  # 空 async generator -> captured_spec None -> RuntimeError

    monkeypatch.setattr(architect_module, "query", fake_query)
    callback = AsyncMock()
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        parse_can_use_tool=callback,
    )
    with pytest.raises(RuntimeError):
        await governor._parse_requirement("做个登录页", context_continuation=False)
    opts = seen["options"]
    assert opts.can_use_tool is callback
    assert "AskUserQuestion" not in (opts.disallowed_tools or [])


async def test_parse_can_use_tool_default_denies_ask():
    """未注入时缺省 _default_can_use_tool：AskUserQuestion 拒绝（无头不悬挂）。"""
    governor = Governor(project_dir=".", session_store=MagicMock(spec=SessionStore))
    assert governor._parse_can_use_tool is _default_can_use_tool


async def test_rebuild_retains_parse_can_use_tool(monkeypatch):
    """rebuild 后注入的 parse_can_use_tool 仍在（字段挂 Governor 不挂 resident）。"""
    callback = AsyncMock()
    governor = Governor(
        project_dir=".",
        session_store=MagicMock(spec=SessionStore),
        parse_can_use_tool=callback,
    )
    monkeypatch.setattr(BaseAgentSession, "start", AsyncMock())
    monkeypatch.setattr(BaseAgentSession, "close", AsyncMock())
    await governor.rebuild()
    assert governor._parse_can_use_tool is callback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_governor.py -v -k "parse_query_wires or parse_can_use_tool_default or rebuild_retains_parse"`
Expected: FAIL（`TypeError: unexpected keyword argument 'parse_can_use_tool'`）

- [ ] **Step 3: Write minimal implementation**

3a. `Governor.__init__`（architect.py:313-338）：签名加 `parse_can_use_tool: CanUseTool | None = None`，`self._can_use_tool` 赋值后加：

```python
        # 交付轨解析 query 的权限回调（chat 层注入；None 时默认拒绝反问，无头不悬挂）。
        self._parse_can_use_tool = (
            parse_can_use_tool
            if parse_can_use_tool is not None
            else _default_can_use_tool
        )
```

3b. `_parse_requirement` opts（architect.py:439-463）：
- `ClaudeAgentOptions(...)` 加一行 `can_use_tool=self._parse_can_use_tool,`
- `disallowed_tools` 列表删 `"AskUserQuestion"`（保留其余），该行注释更新：`# 写工具/任务工具/计划模式全禁;AskUserQuestion 移出,经 can_use_tool 回调反问`

3c. `REQUIREMENT_PARSER_PROMPT` 工作流程第 1 步后（architect.py:161-162 之间）插入一行：

```
   需求关键信息缺失或有歧义且探索代码无法确定时，调用 AskUserQuestion 反问用户（少而精，能靠探索确定的不要问）。
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_governor.py -v`
Expected: 全绿（含既有 governor 测试）

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/core/architect.py tests/test_governor.py
git commit -m "feat: 解析 query 接 can_use_tool 反问通道,AskUserQuestion 移出 disallowed"
```

---

### Task 5: chat 层注入 + 端到端接线 + 版本

**Files:**
- Modify: `src/autoloop_agent/chat/repl.py:121-127`（`_create_session`）
- Modify: `src/autoloop_agent/__init__.py`（`__version__`）
- Test: `tests/test_delivery_ask_question.py`（追加注入断言）

**Interfaces:**
- Consumes: Task 3 `_delivery_can_use_tool`；Task 4 `Governor(parse_can_use_tool=...)`。
- Produces: 无新接口（接线收尾）。

- [ ] **Step 1: Write the failing test**

`tests/test_delivery_ask_question.py` 追加：

```python
def test_governor_injected_with_delivery_callback():
    """ChatCLI._create_session：Governor 挂 _delivery_can_use_tool 为解析回调。"""
    cli = make_cli()
    assert cli.governor._parse_can_use_tool == cli._delivery_can_use_tool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_delivery_ask_question.py::test_governor_injected_with_delivery_callback -v`
Expected: FAIL（`_parse_can_use_tool is _default_can_use_tool`，断言不等）

- [ ] **Step 3: Write minimal implementation**

repl.py `_create_session`：

```python
    def _create_session(self) -> Governor:
        return Governor(
            project_dir=self.project_dir,
            model=self.model,
            session_store=self.session_store,
            can_use_tool=self._can_use_tool,
            parse_can_use_tool=self._delivery_can_use_tool,
        )
```

`src/autoloop_agent/__init__.py`：`__version__` 升到 `0.6.2`。

- [ ] **Step 4: Run full test suite**

Run: `pytest -v`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add src/autoloop_agent/chat/repl.py src/autoloop_agent/__init__.py tests/test_delivery_ask_question.py
git commit -m "feat: 交付轨解析反问可视化 - 问题卡入抽屉+answer 模式复用 + 版本 0.6.2"
```

---

## Self-Review 记录

- Spec 覆盖：architect 改动→Task 4；repl 公共抽取/交付回调/竞态→Task 2/3；drawer 卡片→Task 1；注入→Task 5；测试 6 项→散布 Task 1/3/4/5（drawer 渲染=Task1，数字映射/自定义=Task3 两测试，排队=Task3，ESC=Task3，非 Ask 拒绝=Task3，配置注入=Task4/5）。无缺口。
- 占位符：无 TBD/TODO；所有代码步骤含完整代码。
- 类型一致：`append_question_card(question, options)` Task 1 定义 = Task 3 `render_card` 签名 `Callable[[str, list[str]], None]`；`_parse_can_use_tool` Task 4 定义 = Task 5 消费；`echo: Callable[[str], None]` Task 2 定义 = Task 3 `drawer.append_log`（签名 `text: str`）匹配。
- 风险点：`asyncio.wait([prev])` 传 coroutine 在 3.11 报错——这里传的是 Future（awaitable），合法；`asyncio.wait` DeprecationWarning 仅针对 coroutine，Future 不受影响。
