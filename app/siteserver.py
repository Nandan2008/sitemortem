"""Serve a static site folder on a free local port, plus a tiny mock backend.

The mock /api/stats endpoint exists so the bundled demo site has something real to
call. Any other site just gets static files.
"""
from __future__ import annotations

import json
import mimetypes
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = {"/api/stats": {"total": 42, "latest": "2 lb Ethiopia Guji, 4 minutes ago"}}


def make_handler(root: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in API:
                return self._send(200, json.dumps(API[path]).encode(), "application/json")
            if path == "/":
                path = "/index.html"
            f = (root / path.lstrip("/")).resolve()
            if root.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype)

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return H


class SiteServer:
    def __init__(self, root: Path):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(Path(root)))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start(); return self

    def __exit__(self, *a):
        self.httpd.shutdown(); self.httpd.server_close()
