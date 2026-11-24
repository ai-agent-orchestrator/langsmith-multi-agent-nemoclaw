"""Loopback-only SDK capture and explicit offline mutation recording."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

from langsmith import Client

from .masking import mask_any


@contextmanager
def capture_server() -> Iterator[tuple[str, list[dict]]]:
    """Receive real SDK HTTP traffic on loopback; never forward requests."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append({"method": "GET", "path": self.path, "body": ""})
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append({"method": self.command, "path": self.path, "body": body.decode("utf-8", errors="replace")})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_PATCH = do_POST

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class OfflineClient:
    """Record SDK mutations locally; these records are not claims of cloud execution."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __getattr__(self, method: str) -> Any:
        if not callable(getattr(Client, method, None)):
            raise AttributeError(method)

        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(mask_any({"method": method, "args": args, "kwargs": kwargs}))
            if method.startswith("list_"):
                return []
            if method == "read_dataset":
                return None
            if method == "pull_prompt":
                return None
            return SimpleNamespace(id=uuid4(), status="offline-recorded")

        return record
