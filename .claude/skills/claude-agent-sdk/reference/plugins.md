# 插件（`SdkPluginConfig`）

> 源码: `types.py:824`，`ClaudeAgentOptions.plugins` (`types.py:1844`)

```python
class SdkPluginConfig(TypedDict):
    type: Literal["local"]      # 目前仅支持 local
    path: str

options = ClaudeAgentOptions(plugins=[{"type": "local", "path": "/path/to/plugin"}])
```

插件提供自定义命令、agents、skills、hooks。**星射线当前不使用**--所有能力自建。保留备查。
