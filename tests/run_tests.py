#!/usr/bin/env python3
"""End-to-end test for cellkit: one public port, N services.

Everything is real except the upstreams (fake llama binary + echo servers).
Drives the gateway exactly like a notebook would:

    MCPS = [ck.mcp(...), ck.mcp(...)]
    ck.nginx(port=8890, llama=8080, services=[...])
    ck.mcps(MCPS)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PUBLIC, LLAMA, M1, M2, M3 = 8890, 8080, 8891, 8892, 8893
LLAMA_TOKEN, MCP_TOKEN = "test-token-123", "mcpsecret"
NODE = shutil.which("node") or "/usr/bin/node"

import cellkit as ck  # noqa: E402

PASS, FAIL = [], []


def check(label, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"   {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n         got={got!r}\n        want={want!r}"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def req(method, path, token=None, body=None, port=PUBLIC, headers=None, follow=True, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Accept", "application/json, text/event-stream")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(r, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")
    except Exception as e:
        return 0, {}, f"{type(e).__name__}: {e}"


def jget(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    try:
        return json.loads(text)
    except Exception:
        return {}


# ---------------------------------------------------------------- clean slate
def listeners(port):
    pids = set()
    out = subprocess.run(f"ss -tlnpH 2>/dev/null | grep ':{port} '", shell=True,
                         capture_output=True, text=True).stdout
    pids |= {int(x) for x in re.findall(r"pid=(\d+)", out)}
    if not pids:
        out = subprocess.run(f"lsof -t -iTCP:{port} -sTCP:LISTEN 2>/dev/null", shell=True,
                             capture_output=True, text=True).stdout
        pids |= {int(x) for x in out.split() if x.isdigit()}
    if not pids:
        want, inodes = f"{port:04X}", set()
        for f in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                rows = open(f).read().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                c = row.split()
                if len(c) > 9 and c[1].rsplit(":", 1)[-1] == want and c[3] == "0A":
                    inodes.add(c[9])
        for pid in (os.listdir("/proc") if inodes else []):
            if not pid.isdigit():
                continue
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        t = os.readlink(f"/proc/{pid}/fd/{fd}")
                    except OSError:
                        continue
                    if t.startswith("socket:[") and t[8:-1] in inodes:
                        pids.add(int(pid))
                        break
            except OSError:
                continue
    return pids


subprocess.run("pkill -9 -f '[m]cp-proxy' || true; pkill -9 -f '[m]ock_upstream' || true; "
               "pkill -9 -f '[f]ake_llama' || true", shell=True)
time.sleep(0.5)
for port in (PUBLIC, LLAMA, M1, M2, M3):
    for pid in listeners(port):
        try:
            os.kill(pid, 9)
        except OSError:
            pass
time.sleep(1)
os.makedirs("/kaggle/working", exist_ok=True)

# ------------------------------------------- "your separate llama cell"
llama_proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_llama_server.py"), str(LLAMA)],
                              stdout=open("/tmp/fake_llama_test.log", "w", buffering=1),
                              stderr=subprocess.STDOUT, start_new_session=True)
time.sleep(1)
print(f"[separate llama cell] fake llama-server on 127.0.0.1:{LLAMA}")

# echo upstreams standing in for two MCP proxies + a plain HTTP service
for port, name in ((M1, "mcp1"), (M2, "mcp2"), (M3, "echo-svc")):
    subprocess.Popen([sys.executable, os.path.join(HERE, "mock_upstream.py"), str(port), name],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
time.sleep(1)

# ---------------------------------------------------------------- CELL A
print("\n########## CELL A: ck.nginx(...) ##########")
MCPS = [
    ck.mcp("/mcp", 8891, command=[NODE, os.path.join(HERE, "mock_mcp_stdio.js")]),
    ck.mcp("/mcp/fs", 8892, name="filesystem", token=MCP_TOKEN,
           command=[NODE, os.path.join(HERE, "mock_mcp_stdio.js")]),
]
services = [
    ck.http("/svc/echo", M3),
    ck.http("/raw/echo", M3, strip_prefix=False),
]
st = ck.nginx(port=PUBLIC, llama=LLAMA, token=LLAMA_TOKEN, services=[*MCPS, *services],
              sse_compat=True)          # -> the first mcp service

print("\n--- routing through the single public port ---")
cases = [
    # llama surface (WebUI catch-all + /v1 + exact ops routes)
    ("GET",  "/health",                 None,         200, "llama-server(fake)", "/health", ""),
    ("GET",  "/v1/models",              None,         200, "llama-server(fake)", "/models", ""),
    ("POST", "/v1/chat/completions",    None,         401, None, None, None),
    ("POST", "/v1/chat/completions",    LLAMA_TOKEN,  200, "llama-server(fake)", "/chat/completions", ""),
    ("GET",  "/metrics",                "wrong",      401, None, None, None),
    ("GET",  "/props",                  None,         200, "llama-server(fake)", "/props", ""),   # public: WebUI reads it
    ("GET",  "/metrics",                LLAMA_TOKEN,  200, "llama-server(fake)", "/metrics", ""),
    # MCP mounts
    ("POST", "/mcp",                    None,         200, "mcp1", "/mcp", ""),
    ("GET",  "/mcp",                    None,         200, "mcp1", "/mcp", ""),
    ("GET",  "/mcp/sse",                None,         200, "mcp1", "/sse", ""),
    ("GET",  "/mcp/ping",               None,         200, "mcp1", "/ping", ""),
    ("POST", "/mcp/messages?sessionId=abc", None,     200, "mcp1", "/messages", "sessionId=abc"),
    ("POST", "/mcp/fs",                 None,         401, None, None, None),
    ("POST", "/mcp/fs",                 MCP_TOKEN,    200, "mcp2", "/mcp", ""),
    ("GET",  "/mcp/fs/sse",             MCP_TOKEN,    200, "mcp2", "/sse", ""),
    ("GET",  "/mcp/fs/ping",            MCP_TOKEN,    200, "mcp2", "/ping", ""),
    ("POST", "/mcp/fs/messages?sessionId=zz", MCP_TOKEN, 200, "mcp2", "/messages", "sessionId=zz"),
    # generic http service
    ("GET",  "/svc/echo/abc?x=1",       None,         200, "echo-svc", "/abc", "x=1"),
    ("GET",  "/svc/echo/deep/path",     None,         200, "echo-svc", "/deep/path", ""),
    ("GET",  "/raw/echo/abc",           None,         200, "echo-svc", "/raw/echo/abc", ""),
    # legacy SSE compat at the origin
    ("GET",  "/sse",                    None,         200, "mcp1", "/sse", ""),
    ("POST", "/messages?sessionId=orig", None,        200, "mcp1", "/messages", "sessionId=orig"),
]
for method, path, token, status, upstream, upath, query in cases:
    code, _, body = req(method, path, token)
    label = f"{method} {path}" + (f"  [{token}]" if token else "")
    check(f"{label} -> {status}", code, status)
    if upstream:
        j = jget(body)
        check(f"{label} -> {upstream}{upath}", (j.get("name"), j.get("path"), j.get("query")),
              (upstream, upath, query))

# the WebUI (catch-all at "/") + everything else still routing correctly
code, _, body = req("GET", "/")
check("GET / -> WebUI (catch-all HTML)", (code, "webui-html" in body), (200, True))
code, _, body = req("GET", "/bundle.js")
check("GET /bundle.js -> WebUI asset", (code, "bundle.js" in body), (200, True))
check("catch-all doesn't swallow /mcp", jget(req("POST", "/mcp")[2]).get("name"), "mcp1")
check("catch-all doesn't swallow /mcp/fs", jget(req("POST", "/mcp/fs", MCP_TOKEN)[2]).get("name"), "mcp2")
check("catch-all doesn't swallow /v1/models", jget(req("GET", "/v1/models")[2]).get("path"), "/models")
check("catch-all doesn't swallow /health", jget(req("GET", "/health")[2]).get("status"), "ok")
check("UI ungated while /v1 gated", (req("GET", "/")[0], req("POST", "/v1/chat/completions")[0]), (200, 401))
code, _, body = req("GET", "/v1", LLAMA_TOKEN)
check("GET /v1 (authed) -> backend root (WebUI page)", (code, "webui-html" in body), (200, True))

# mcp-session-id pass-through
code, _, body = req("DELETE", "/mcp", headers={"mcp-session-id": "sess-42"})
check("DELETE /mcp forwards mcp-session-id", (code, jget(body).get("session")), (200, "sess-42"))

# discovery + tokens in state
j = jget(req("GET", "/_gateway")[2])
check("GET /_gateway lists every route",
      sorted(r for s in j.get("services", []) for r in s["routes"]),
      sorted(["/", "/v1", "/health", "/props", "/metrics", "/slots", "/tokenize",
              "/detokenize", "/mcp", "/mcp/fs", "/svc/echo", "/raw/echo"]))
check("state.json has mcp entries + tokens",
      (len(st["mcp"]), st["tokens"].get("llama_api")), (2, LLAMA_TOKEN))
check("ck.status() reports the same table", ck.status()["public_port"], PUBLIC)

# SSE must stream tick by tick, not buffered
t0, stamps = time.time(), []
with urllib.request.urlopen(f"http://127.0.0.1:{PUBLIC}/mcp/stream", timeout=20) as r:
    for line in r:
        if line.strip().startswith(b"data:"):
            stamps.append(round(time.time() - t0, 2))
check("SSE streams through nginx (3 events, not buffered)",
      (len(stamps), stamps[0] < 0.5, stamps[-1] > 0.6), (3, True, True))
print(f"         event arrival times: {stamps}")

# ---------------------------------------------------------------- CELL B
print("\n########## CELL B: ck.mcps(MCPS) ##########")
# node 20 is what's in this sandbox; a Kaggle image gets node 22 downloaded
node_paths = {"node": NODE, "npm": shutil.which("npm"), "npx": shutil.which("npx")}
ck.ensure_node = lambda **kw: node_paths          # noqa: ARG005  (test stub)
run = ck.mcps(MCPS)

print("\n--- full stack: nginx -> mcp-proxy -> stdio MCP ---")
check("both MCPs reported running (name auto-derived from the command)",
      sorted(r["name"] for r in run), ["filesystem", "mock_mcp_stdio"])


def call(path, method, params=None, token=None, session=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    hdrs = {"Mcp-Session-Id": session} if session else {}
    code, h, text = req("POST", path, token, body=body, headers=hdrs)
    sid = h.get("Mcp-Session-Id") or session
    return code, jget(text), sid


for route, token in (("/mcp", None), ("/mcp/fs", MCP_TOKEN)):
    code, j, sid = call(route, "initialize",
                        {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}, token)
    check(f"POST {route} initialize via public port",
          (code, (j.get("result") or {}).get("serverInfo", {}).get("name")),
          (200, "mock-stdio-mcp"))
    check(f"POST {route} returns Mcp-Session-Id through nginx", bool(sid), True)
    code, j, _ = call(route, "tools/list", token=token, session=sid)
    check(f"POST {route} tools/list", (code, [t["name"] for t in (j.get("result") or {}).get("tools", [])]),
          (200, ["mock_echo"]))
    code, j, _ = call(route, "tools/call", {"name": "mock_echo", "arguments": {"text": "hi"}},
                      token=token, session=sid)
    check(f"POST {route} tools/call",
          (code, ((j.get("result") or {}).get("content") or [{}])[0].get("text")), (200, "pong:hi"))
    code, _, _ = call(route, "tools/list", token=token)
    check(f"POST {route} tools/list without session -> 400", code, 400)

check("POST /mcp/fs without token is rejected",
      req("POST", "/mcp/fs", None, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0], 401)
check("llama (other cell) never disturbed", llama_proc.poll(), None)

# ---------------------------------------------------------------- config sanity
conf = open(st["conf"]).read()
check("exactly one server block", conf.count("server {"), 1)
check("one upstream per service", conf.count("upstream svc_"), len(st["services"]))
check("catch-all emitted as plain `location /`",
      "location / {\n        proxy_pass http://svc_llama_ui;" in conf, True)
check("discovery at /_gateway, not /",
      ("location = /_gateway" in conf,
       "location = / {\n            default_type application/json" in conf), (True, False))
check("no nested if in any auth gate", "; if (" in conf, False)
check("token map uses one case-insensitive key", conf.count("~*^Bearer[[:space:]]+"), 3)

# ---------------------------------------------------------------- summary
print("\n" + "=" * 60)
print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
for f in FAIL:
    print("  FAILED:", f)
print("=" * 60)
print("\n--- access log tail ---")
print(subprocess.run(f"tail -n 8 {ck.DEFAULT_DIR}/nginx_access.log", shell=True,
                     capture_output=True, text=True).stdout)
sys.exit(1 if FAIL else 0)
