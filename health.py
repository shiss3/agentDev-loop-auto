"""健康检查模块 — 暴露 GET /health 接口，返回 HTTP 200 + 健康状态 JSON。

使用 Python 标准库 http.server，无需额外依赖。
可直接被 tests/test_health.py 导入测试，也可通过 `python health.py` 启动独立服务。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def health_status() -> dict[str, Any]:
    """返回健康状态字典。

    Returns:
        包含 ``status`` 等字段的 dict，表示当前服务健康状态。
    """
    return {
        "status": "ok",
        "service": "harness-agent",
    }


class HealthHandler(BaseHTTPRequestHandler):
    """处理 GET /health 请求的 HTTP handler。"""

    def do_GET(self) -> None:  # noqa: N802 – http.server 约定方法名
        if self.path == "/health":
            body = json.dumps(health_status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 – 签名由基类决定
        """静默日志，避免测试输出噪声。"""
        return


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """创建并返回 HTTP 服务实例（不启动）。"""
    return ThreadingHTTPServer((host, port), HealthHandler)


def main() -> None:
    """启动健康检查 HTTP 服务。"""
    server = create_server()
    host, port = server.server_address[:2]
    print(f"health check service on http://{host}:{port}/health")  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
