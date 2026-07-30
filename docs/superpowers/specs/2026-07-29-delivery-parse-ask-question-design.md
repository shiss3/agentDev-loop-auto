# 交付轨解析反问可视化设计

日期：2026-07-29
状态：已批准（方案 A）

## 背景

TUI 双轨：左侧交互轨（仿 claude code cli），右侧交付轨抽屉（DrawerPanel）FIFO 解析执行需求。
交互轨已支持 `AskUserQuestion` 反问：`repl.py:_can_use_tool` 回调拦截 → `content_buffer.append_question_card` 渲染问题卡 → Future + answer 模式路由用户输入 → `PermissionResultAllow(updated_input=answers)` 回注。

交付轨解析（`architect.py:_parse_requirement`，stateless `query()`）当前把 `AskUserQuestion` 列在 `disallowed_tools`，解析遇歧义只能猜或降级，无法反问。

## 目标

交付轨 FIFO 解析需求时，解析 query 中模型调用 `AskUserQuestion` 可被识别、问题卡渲染进右侧交付轨抽屉、用户经共享输入框作答、答案回注后解析继续。

## 方案

选 **A：can_use_tool 回调**（否决 B：SDK MCP ask 工具——与 AskUserQuestion 双通道不一致；否决 C：解析外澄清——无流式体验、重跑费 token）。

`ClaudeAgentOptions` 支持 `can_use_tool`，解析 query 与常驻执行体同机制，改动最小。

## 组件改动

### 1. `src/autoloop_agent/core/architect.py`

- `_parse_requirement`：
  - `disallowed_tools` 移除 `AskUserQuestion`；`allowed_tools` **不加**它（不预放行，走权限回调）。
  - 新增可选参数 `can_use_tool`（默认 None → 用拒绝反问的默认回调，保无头/测试调用可用），传入 `ClaudeAgentOptions`。
- `REQUIREMENT_PARSER_PROMPT`：补一句——需求关键信息缺失/歧义时可调用 AskUserQuestion 反问用户，反问应少而精。
- `Governor` 构造新增可选参数 `parse_can_use_tool`（chat 层注入，rebuild 不丢，参照现有 `can_use_tool` 字段模式）；`parse_flow` / `handle_user_input`（forced_track="delivery" 分支）调 `_parse_requirement` 时透传。

### 2. `src/autoloop_agent/chat/repl.py`

- 抽公共方法 `_ask_questions(tool_input, render_card, render_echo)`：
  - 渲染卡（渲染目标作回调参数：交互轨→`content_buffer.append_question_card`，交付轨→drawer 问题卡）。
  - **竞态**：`_pending_answer` 非空时先 await 其完成再占槽（后到排队等待）。
  - 建 Future → `tui.set_answer_mode(True)` → 逐问题 await → 收集 answers → `PermissionResultAllow(updated_input)`；CancelledError → `PermissionResultDeny`。
- 现有 `_can_use_tool` 改为薄壳：非 AskUserQuestion 放行，否则调 `_ask_questions`（渲染目标=content_buffer）。
- 新增 `_delivery_can_use_tool`：非 AskUserQuestion **拒绝**（保解析只读语义），AskUserQuestion 调 `_ask_questions`（渲染目标=drawer，回显 `回答> x` 也进 drawer）。
- `_create_session` 注入 `parse_can_use_tool=self._delivery_can_use_tool`。

### 3. `src/autoloop_agent/chat/drawer.py`

- 新增 `append_question_card(question, options)`：样式对齐 content_buffer 同款（`┌ 解析反问 ─ / │ 问题 / │ 1. 选项 / └ 输入编号选择…`），多行进抽屉日志。

### 4. answer 路由

复用全局 answer 模式：`_on_user_input` 现有 `_pending_answer` 分支不变，无需区分提问来自哪一轨（槽位独占，排队见上）。

## 数据流

```
模型 tool_use(AskUserQuestion)
  → SDK 权限回调 _delivery_can_use_tool
  → _ask_questions: 等槽 → drawer.append_question_card → 占 _pending_answer
  → 用户输入(answer 模式,数字=选项/文字=自定义)
  → answers 回注 PermissionResultAllow(updated_input)
  → 模型继续解析 → submit_analysis_plan 收尾
```

## 错误处理

- ESC 取消：Future 取消 → Deny("用户取消了提问")，解析 query 收到拒绝后继续或结束；`captured_spec is None` 走现有 RuntimeError 降级路径。
- 回调异常：`_parse_requirement` 现有 try/except 降级为交互轨，不变。
- 无头调用（`parse_can_use_tool=None`）：默认回调拒绝反问，模型自行决策，行为同现状。

## 测试

仿 `tests/test_ask_user_question.py`：

1. 交付轨回调拦 AskUserQuestion → 问题卡行进 drawer，不进 content_buffer。
2. 数字输入映射选项 label，答案经 `updated_input.answers` 回注。
3. 自定义文字答案直通。
4. 竞态：交互轨 pending answer 占用时，交付轨提问排队，前一答完后才占槽。
5. ESC 取消 → Deny，Future 不悬挂，answer 模式复位。
6. 非 AskUserQuestion 工具 → 拒绝。

## 不做（YAGNI）

- 双轨独立 answer 槽。
- 反问次数硬上限（靠 prompt 约束）。
- 问题历史持久化。
