"""GET /health 端点测试。

覆盖:
- GET /health 返回 200
- 响应含 status 字段且值为 "ok"
- 响应含 service 字段
- 非健康路径返回 404
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from typing import Generator

import pytest

from health import HealthHandler, create_server, health_status


@pytest.fixture()
def server() -> Generator[str, None, None]:
    """启动临时 HTTP 服务,返回 base_url。"""
    # 选空闲端口避免冲突
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = create_server("127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict]:
    """发起 GET 请求,返回 (status_code, json_body)。"""
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        body = json.loads(resp.read().decode("utf-8"))
        return resp.status, body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8")) if exc.read else {}
        return exc.code, body


def test_health_returns_200(server: str) -> None:
    """GET /health 应返回 HTTP 200。"""
    status, _ = _get(f"{server}/health")
    assert status == 200


def test_health_response_has_status_field(server: str) -> None:
    """GET /health 响应应含 status 字段且值为 'ok'。"""
    _, body = _get(f"{server}/health")
    assert "status" in body
    assert body["status"] == "ok"


def test_health_response_has_service_field(server: str) -> None:
    """GET /health 响应应含 service 字段。"""
    _, body = _get(f"{server}/health")
    assert "service" in body
    assert body["service"] == "harness-agent"


def test_health_status_function() -> None:
    """health_status() 应返回包含 status='ok' 的 dict。"""
    result = health_status()
    assert result["status"] == "ok"
    assert "service" in result


def test_unknown_path_returns_404(server: str) -> None:
    """非 /health 路径应返回 404。"""
    status, body = _get(f"{server}/unknown")
    assert status == 404


def test_handler_class_exists() -> None:
    """HealthHandler 类应可导入。"""
    assert HealthHandler is not None
