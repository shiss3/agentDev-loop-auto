"""Harness Agent 配置管理"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChatConfig:
    """Chat 模式配置

    Attributes:
        max_turns:    单轮对话最大 Agent 交互轮次
        history_file: 输入历史文件路径（None 表示用默认 ~/.harness/chat_history）
        show_usage:   是否显示 Token 用量
        show_timing:  是否显示耗时
    """

    max_turns: int | None = None  # None 表示无限制，由上下文长度自然限制
    history_file: str | None = None
    show_usage: bool = True
    show_timing: bool = True


@dataclass
class HarnessConfig:
    """全局配置

    Attributes:
        project_dir: 项目工作目录
        max_turns: Agent 最大交互轮次
        allowed_tools: 允许的工具列表
        log_level: 日志级别
        chat: Chat 模式专属配置
    """

    project_dir: str = "."
    max_turns: int = 15
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash"]
    )
    log_level: str = "INFO"
    chat: ChatConfig = field(default_factory=ChatConfig)

    def __post_init__(self):
        self.project_dir = str(Path(self.project_dir).resolve())
