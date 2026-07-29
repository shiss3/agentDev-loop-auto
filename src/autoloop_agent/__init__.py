"""AutoLoop - 自动循环开发"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("autoloop-agent")
except PackageNotFoundError:
    __version__ = "0.6.2"
