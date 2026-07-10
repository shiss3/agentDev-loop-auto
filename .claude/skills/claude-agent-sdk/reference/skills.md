# Skills（★ 澄清: 是 SDK 原生能力，非 CLI 专属）

> 源码: `ClaudeAgentOptions.skills` (`types.py:1812`)，`AgentDefinition.skills` (`types.py:93`)

## 主会话 skills

```python
options = ClaudeAgentOptions(
    skills=["my-skill", "plugin:skill-name"]   # list[str]：只启用列出的
    # 或 skills="all"                            # 启用所有发现的 skill
    # 或 skills=None (默认)                      # 不做 SDK 自动配置，CLI 默认仍生效
    # 或 skills=[]                                # 抑制所有 skill
)
```

SDK 文档原文（`types.py:1812`）: *"This is the single place to turn skills on; you do not need to add `"Skill"` to `allowed_tools` or set `setting_sources` yourself - the SDK does both when this is set."*

- 名字匹配 `SKILL.md` 的 `name` / 目录名，插件 skill 用 `plugin:skill` 形式。
- **是上下文过滤器，不是沙箱**: 未列出的 skill 对模型隐藏、被 Skill 工具拒绝，但其文件仍在磁盘上，可被 `Read`/`Bash` 访问。不要在 skill 文件里存密钥。

## subagent skills

`AgentDefinition.skills: list[str] | None`（`types.py:93`）--subagent 也能挂 skills。

## `allowed_tools` 弃用说明

`ClaudeAgentOptions.allowed_tools` docstring（`types.py:1599`）: 传 `"Skill"` 已弃用，改用 `skills` 选项。`AgentDefinition.tools` 同理（`types.py:88`）。

## 星射线落地

Phase 4 的 Skill 知识库（`.skills/` + `SkillsAgent`）规划用此能力。当前 Phase 2 **不启用** skills。但核对结论修正了早期判断: **skills 是 SDK Python API 的原生能力**，不是只能通过 CLI 使用的概念。
