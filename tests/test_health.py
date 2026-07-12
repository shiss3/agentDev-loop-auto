"""tests/test_health.py — /health 接口测试。

验证:
  1. health_response() 返回 {"status": "ok"}
  2. GET /health 返回 200 + 正确 JSON 结构
  3. GET /unknown 返回 404
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from typing import Generator

import pytest

from health import create_server, health_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """获取一个可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server() -> Generator:
    """启动临时 HTTP 服务器，测试结束后关闭。"""
    port = _free_port()
    srv = create_server("127.0.0.1", port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()
    srv.server_close()


def _get(port: int, path: str) -> tuple[int, dict]:
    """发起 GET 请求，返回 (status_code, json_body)。"""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body


# ---------------------------------------------------------------------------
# 单元测试: health_response()
# ---------------------------------------------------------------------------

class TestHealthResponse:
    """直接测试 health_response() 函数。"""

    def test_returns_ok_status(self) -> None:
        result = health_response()
        assert result == {"status": "ok"}

    def test_status_is_string(self) -> None:
        result = health_response()
        assert isinstance(result["status"], str)

    def test_has_status_key(self) -> None:
        result = health_response()
        assert "status" in result


# ---------------------------------------------------------------------------
# 集成测试: GET /health (HTTP)
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """通过真实 HTTP 请求测试 /health 接口。"""

    def test_health_returns_200(self, server: int) -> None:
        port = server
        status, _ = _get(port, "/health")
        assert status == 200

    def test_health_returns_json_with_status_ok(self, server: int) -> None:
        port = server
        _, body = _get(port, "/health")
        assert body == {"status": "ok"}

    def test_health_content_type_is_json(self, server: int) -> None:
        """验证响应头 Content-Type 为 application/json。"""
        port = server
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.headers["Content-Type"] == "application/json"

    def test_unknown_path_returns_404(self, server: int) -> None:
        port = server
        status, body = _get(port, "/unknown")
        assert status == 404
        assert "error" in body
