# Hooks（Phase 2 P2 / Phase 3）

> 源码: `HookMatcher` (`types.py:584`)，`ClaudeAgentOptions.hooks` (`types.py:1760`)，Hook 输入类型 (`types.py:273+`)

## `HookEvent` 取值（`types.py:259`）

`PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `UserPromptSubmit` / `Stop` / `SubagentStop` / `PreCompact` / `Notification` / `SubagentStart` / `PermissionRequest`

## `HookMatcher`

```python
@dataclass
class HookMatcher:                                  # types.py:584
    matcher: str | None = None                      # 工具名，如 "Bash" 或 "Write|Edit|MultiEdit"
    hooks: list[HookCallback] = field(default_factory=list)
    timeout: float | None = None                    # 秒，默认 60
```

## 用法

```python
options = ClaudeAgentOptions(hooks={
    "PreToolUse": [HookMatcher(
        matcher="Write|Edit",
        hooks=[my_guard_callback],
    )],
})
```

**派发顺序**（`types.py:1766` 原文）: 同一事件上的多个 matcher **并发派发**，不保证顺序--每个 hook 须独立，不能依赖另一个先完成。

## 星射线落地

- `PreToolUse` -> 危险工具护栏（`core/guard.py`，P2 预演）/ 代码审查拦截 / 沙箱回滚（Phase 3）
- `PreCompact` -> 压缩前抢救耐久事实写入 GCH
- `SubagentStart`/`SubagentStop` -> 编排进度可视化
