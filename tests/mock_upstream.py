"""Tiny echo HTTP server used by the tests.

Stands in for llama-server and for an MCP proxy: it answers with the exact
method / path / query it received, so a test can prove what nginx forwarded.

`resolve(method, path)` (see tests/llama_routes.py) turns it into a *strict*
backend that only answers the paths its real counterpart serves -- an echo
server happily accepts any rewrite, which is exactly how a prefix-stripping bug
slips through a suite like this one.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def make_handler(port, name, resolve=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_raw(self, code, ctype, body):
            body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, ticks=3, gap=0.4):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for i in range(ticks):
                self.wfile.write(f"event: tick\ndata: {i}\n\n".encode())
                self.wfile.flush()
                time.sleep(gap)

        def _echo(self, u):
            return {
                "name": name,
                "port": port,
                "method": self.command,
                "path": u.path,
                "query": u.query,
                "auth": self.headers.get("Authorization", ""),
                "session": self.headers.get("Mcp-Session-Id") or self.headers.get("mcp-session-id") or "",
                "host": self.headers.get("Host", ""),
            }

        def _handle(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)

            if resolve is not None:                       # strict backend
                hit = resolve(self.command, u.path)
                if hit is None:
                    return self._send(404, {"error": {"message": "File Not Found",
                                                       "type": "not_found_error", "code": 404}})
                status, kind, payload = hit
                if kind == "sse":
                    return self._sse()
                if kind == "html":
                    return self._send_raw(status, "text/html", payload)
                if kind == "js":
                    return self._send_raw(status, "application/javascript", payload)
                if status != 200:
                    return self._send(status, payload)
                return self._send(status, {**self._echo(u), **payload})

            if u.path == "/stream":          # SSE: 3 events, 0.4s apart
                return self._sse()
            payload = self._echo(u)
            if u.path == "/health":
                payload["status"] = "ok"
            return self._send(200, payload)

        do_GET = do_POST = do_DELETE = do_OPTIONS = do_PUT = _handle

    return Handler


def serve(port, name, resolve=None):
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(port, name, resolve)).serve_forever()


if __name__ == "__main__":
    p = int(sys.argv[1])
    serve(p, sys.argv[2] if len(sys.argv) > 2 else f"mock-{p}")
