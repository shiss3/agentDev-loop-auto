"""Harness Agent - 驾驭工程实践"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("harness-agent")
except PackageNotFoundError:
    __version__ = "0.0.0"
