# agentDev-autoLoop

> 基于 Claude Agent SDK 的需求自动循环开发系统

## 安装 (使用 pip)

本项目使用现代化的 `pyproject.toml` 作为配置中心。

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境 (Windows PowerShell)
.\.venv\Scripts\activate

# 3. 安装依赖（以开发模式安装）
pip install -e ".[dev]"
```

## 环境变量配置

在根目录下将 `.env.example` 复制为 `.env`，并填入您的 API Key：
```bash
cp .env.example .env
# 然后编辑 .env 文件，填入 ANTHROPIC_API_KEY
```

## 使用 CLI

确保虚拟环境已激活后，您可以直接运行 `autoloop` 命令：

```bash
# 查看版本
autoloop --version

# 执行开发任务
autoloop run "创建一个 hello_world.py"

# 指定项目目录运行
autoloop run "重构 user.py" --project ./my-app
```

## 运行测试

确保虚拟环境已激活后，使用 `pytest` 运行测试：

```bash
# 运行全部测试
pytest tests/test_smoke.py -v

# 运行需要 API Key 的请求和 Caching 探测测试
pytest tests/test_smoke.py::test_sdk_simple_query -v -s
pytest tests/test_smoke.py::test_prompt_caching_support -v -s
```

## 技术栈

- **Agent 执行**: claude-agent-sdk (Python)
- **编排引擎**: LangGraph
- **CLI**: Click + Rich
