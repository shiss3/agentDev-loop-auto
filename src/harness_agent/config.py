"""Harness Agent 配置管理"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HarnessConfig:
    """全局配置

    Attributes:
        project_dir: 项目工作目录
        max_turns: Agent 最大交互轮次
        allowed_tools: 允许的工具列表
        log_level: 日志级别
    """

    project_dir: str = "."
    max_turns: int = 15
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash"]
    )
    log_level: str = "INFO"

    def __post_init__(self):
        self.project_dir = str(Path(self.project_dir).resolve())
