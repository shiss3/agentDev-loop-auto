# 权限：PermissionMode 与 can_use_tool

> 工具可见性字段 `tools` / `allowed_tools` / `disallowed_tools` 的定义见 [options.md](options.md) 的「工具与权限」节。

## `PermissionMode` 取值

> 源码: `types.py:24`

```python
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
```

| 值 | 行为 |
|----|------|
| `default` | 标准行为，危险操作弹权限 |
| `acceptEdits` | 自动接受文件编辑 |
| `plan` | 计划模式，不执行工具 |
| `bypassPermissions` | 绕过所有权限检查 |
| `dontAsk` | 不弹窗，未预批准即拒绝 |
| `auto` | 模型分类器决定每次工具调用允许/拒绝 |

> 注: `ClaudeAgentOptions.permission_mode` 的 docstring（`types.py:1629`）只列了前 5 个，但 `types.py:24` 的 Literal 和 `query.py` docstring 都含 `auto`。以 Literal 定义为准。

## 动态权限: `can_use_tool` + `PermissionResult`（★ Phase 2 核心）

> 源码: `types.py:197-255`

### 回调签名

```python
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],   # (tool_name, tool_input, context)
    Awaitable[PermissionResult]
]
```

仅当 CLI 权限规则判定为 "ask" 时触发；`allowed_tools` / `permission_mode=acceptEdits|bypassPermissions` / settings 的 `permissions.allow` 已批准的工具**不会**触发此回调。要观察/拦截**每一次**工具调用应改用 `PreToolUse` hook。

### `ToolPermissionContext` 字段（`types.py:198`）

`signal` / `suggestions` / `tool_use_id` / `agent_id`(sub-agent 内) / `blocked_path` / `decision_reason` / `title`(完整权限句) / `display_name`(短标签) / `description`(副标题)

### 返回值

```python
@dataclass
class PermissionResultAllow:                       # types.py:234
    behavior: Literal["allow"] = "allow"
    updated_input: dict | None = None              # 改写后的工具入参
    updated_permissions: list[PermissionUpdate] | None = None

@dataclass
class PermissionResultDeny:                        # types.py:242
    behavior: Literal["deny"] = "deny"
    message: str = ""
    interrupt: bool = False                        # True=同时中断整个会话

PermissionResult = PermissionResultAllow | PermissionResultDeny
```

### 星射线落地

- **Fast Lane**: `can_use_tool` 返回 `PermissionResultAllow(behavior="allow")`--Fast Lane 在常驻会话里有写权限，全放行。
- **Full mode**: **不走 SDK 的 `can_use_tool`**。Full mode 用 `claude -p` CLI 子进程（独立 worktree），权限通过 CLI flag（`--allowedTools` / `--disallowedTools` / `--permission-mode`）控制--deny `Write`/`Edit`/`NotebookEdit`，allow `Bash`。SDK 的 `can_use_tool` 只对 SDK 进程内的会话生效，CLI 子进程不经过。
