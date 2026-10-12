"""Tiny echo HTTP server used by the tests.

Stands in for llama-server and for an MCP proxy: it answers with the exact
method / path / query it received, so a test can prove what nginx forwarded.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def make_handler(port, name):
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

        def _handle(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            # a stand-in for llama-server's embedded WebUI (a static SPA at "/")
            if u.path in ("/", "/index.html"):
                return self._send_raw(200, "text/html",
                                      f"<html><title>{name}</title>webui-html</html>")
            if u.path == "/bundle.js":
                return self._send_raw(200, "application/javascript",
                                      f"/* {name} bundle.js */")
            if u.path == "/stream":          # SSE: 3 events, 0.4s apart
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for i in range(3):
                    self.wfile.write(f"event: tick\ndata: {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.4)
                return
            payload = {
                "name": name,
                "port": port,
                "method": self.command,
                "path": u.path,
                "query": u.query,
                "auth": self.headers.get("Authorization", ""),
                "session": self.headers.get("Mcp-Session-Id") or self.headers.get("mcp-session-id") or "",
                "host": self.headers.get("Host", ""),
            }
            if u.path == "/health":
                payload["status"] = "ok"
            return self._send(200, payload)

        do_GET = do_POST = do_DELETE = do_OPTIONS = do_PUT = _handle

    return Handler


def serve(port, name):
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(port, name)).serve_forever()


if __name__ == "__main__":
    p = int(sys.argv[1])
    serve(p, sys.argv[2] if len(sys.argv) > 2 else f"mock-{p}")
