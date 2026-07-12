"""健康检查端点 — 暴露 GET /health 接口，返回 200 + JSON 健康状态。

使用标准库 http.server 实现，无需额外依赖。
可直接运行: python health.py  (默认 0.0.0.0:8000)
也可被测试导入: from health import app
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def health_response() -> dict[str, Any]:
    """返回健康状态字典。"""
    return {"status": "ok"}


class HealthHandler(BaseHTTPRequestHandler):
    """处理 /health 请求的 HTTP handler。"""

    def do_GET(self) -> None:  # noqa: N802 — http.server 约定
        if self.path == "/health":
            body = json.dumps(health_response()).encode("utf-8")
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

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默日志
        pass


def create_server(host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    """创建并返回 HTTPServer 实例。"""
    return ThreadingHTTPServer((host, port), HealthHandler)


# 便捷别名，供测试导入
app = HealthHandler


if __name__ == "__main__":
    server = create_server()
    print("Health check server running on http://0.0.0.0:8000/health")
    server.serve_forever()
