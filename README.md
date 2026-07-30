# agentDev-autoLoop

> 基于 Claude Agent SDK 的 需求解析 - 需求调度 - 需求执行 的自动循环开发系统

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

本地已经配置好 claude code 则忽略此项，自动走本地配置。

在根目录下将 `.env.example` 复制为 `.env`，并填入您的 API Key：
```bash
cp .env.example .env
# 然后编辑 .env 文件，填入 ANTHROPIC_API_KEY
```

## 若单纯作为需求解析使用

在 .env 文件中加入字段：HARNESS_DELIVERY_DRY_RUN=1 与 HARNESS_PARSE_LOG=1

字段 1 表示需求解析完阻止拉起 claude code 执行器

字段 2 表示显示需求解析的日志，可以看到解析出的提示词

两个字段删除或值设置为 0 则正常使用

## 使用 CLI

确保虚拟环境已激活后，您可以直接运行 `autoloop` 命令：

```bash
# 查看版本
autoloop --version

# 执行开发任务
autoloop chat

## 模式切换
命令 /auto 默认模式，输入自然语言后进入需求解析，根据需求大小，决定自动拉起 claude code 执行或者直接在本窗口执行

命令 /semi 半自动模式，等于 claude code 在本窗口直接执行与 cc 表现一致，自动集成您本地给 cc 配置的 harness 环境

半自动模式支持与默认模式交互，在半自动模式可以分析出的计划，可以调用本项目自定义需求解析工具，解析结果和默认模式无异，在半自动模式确认需求解析的方案后，自动进入自动模式派发 cc
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
