#!/usr/bin/env python3
"""End-to-end test for cellkit: one public port, N services.

Everything is real except the upstreams (fake llama binary + echo servers).
Drives the gateway exactly like a notebook would:

    MCPS = [ck.mcp(...), ck.mcp(...)]
    ck.nginx(port=8890, llama=8080, services=[...])
    ck.mcps()  # loads the same launch settings from CELL A's state
"""
import importlib
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
NO_UI = 8099          # second fake llama, started with --no-ui, for the origin probe
LLAMA_TOKEN, MCP_TOKEN = "test-token-123", "mcpsecret"
NODE = shutil.which("node") or "/usr/bin/node"

import cellkit as ck  # noqa: E402
from cellkit.gateway import _normalise  # noqa: E402

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
for port in (PUBLIC, LLAMA, M1, M2, M3, NO_UI):
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
FAKE = "llama-server(fake)"
cases = [
    # llama surface (WebUI catch-all + identity-mounted /v1 + exact ops routes).
    # `/v1/...` has to reach llama with its prefix intact: llama.cpp registers the
    # OpenAI routes under /v1 (tests/llama_routes.py), so a mount that strips it
    # 404s on every /v1-only route and lands /v1/completions + /v1/embeddings on
    # llama's *legacy* handlers, which answer with a different schema.
    # (method, path, token, status, upstream, path at the backend, query, handler)
    ("GET",  "/health",                 None,         200, FAKE, "/health", "", "get_health"),
    ("GET",  "/v1/models",              None,         200, FAKE, "/v1/models", "", "get_models"),
    ("GET",  "/v1/models?meta=1",       None,         200, FAKE, "/v1/models", "meta=1", "get_models"),
    ("POST", "/v1/chat/completions",    None,         401, None, None, None, None),
    ("POST", "/v1/chat/completions",    LLAMA_TOKEN,  200, FAKE, "/v1/chat/completions", "", "post_chat_completions"),
    ("POST", "/v1/completions",         LLAMA_TOKEN,  200, FAKE, "/v1/completions", "", "post_completions_oai"),
    ("POST", "/v1/embeddings",          LLAMA_TOKEN,  200, FAKE, "/v1/embeddings", "", "post_embeddings_oai"),
    # /v1-only routes -- a stripping mount sends these to paths llama never serves
    ("POST", "/v1/streams/lookup",      LLAMA_TOKEN,  200, FAKE, "/v1/streams/lookup", "", "streams_lookup"),
    ("POST", "/v1/chat/completions/control", LLAMA_TOKEN, 200, FAKE, "/v1/chat/completions/control", "", "post_control"),
    ("POST", "/v1/messages",            LLAMA_TOKEN,  200, FAKE, "/v1/messages", "", "post_anthropic_messages"),
    ("POST", "/v1/messages/count_tokens", LLAMA_TOKEN, 200, FAKE, "/v1/messages/count_tokens", "", "post_anthropic_count_tokens"),
    ("POST", "/v1/audio/transcriptions", LLAMA_TOKEN, 200, FAKE, "/v1/audio/transcriptions", "", "post_transcriptions_oai"),
    ("POST", "/v1/systemone",           LLAMA_TOKEN,  200, FAKE, "/v1/systemone", "", "post_systemone"),
    ("POST", "/v1/responses",           LLAMA_TOKEN,  200, FAKE, "/v1/responses", "", "post_responses_oai"),
    # the API base is a prefix, not an endpoint: it must not be rewritten to "/"
    ("GET",  "/v1",                     LLAMA_TOKEN,  404, None, None, None, None),
    # ops routes, exact, one public pair for the WebUI to poll
    ("GET",  "/metrics",                "wrong",      401, None, None, None, None),
    ("GET",  "/props",                  None,         200, FAKE, "/props", "", "get_props"),
    ("GET",  "/metrics",                LLAMA_TOKEN,  200, FAKE, "/metrics", "", "get_metrics"),
    ("POST", "/slots/3",                None,         200, FAKE, "/slots/3", "", "post_slot"),   # = /slots does not swallow subpaths
    # MCP mounts (these two backends stay plain echoes: no route table to honour)
    ("POST", "/mcp",                    None,         200, "mcp1", "/mcp", "", None),
    ("GET",  "/mcp",                    None,         200, "mcp1", "/mcp", "", None),
    ("GET",  "/mcp/sse",                None,         200, "mcp1", "/sse", "", None),
    ("GET",  "/mcp/ping",               None,         200, "mcp1", "/ping", "", None),
    ("POST", "/mcp/messages?sessionId=abc", None,     200, "mcp1", "/messages", "sessionId=abc", None),
    ("POST", "/mcp/fs",                 None,         401, None, None, None, None),
    ("POST", "/mcp/fs",                 MCP_TOKEN,    200, "mcp2", "/mcp", "", None),
    ("GET",  "/mcp/fs/sse",             MCP_TOKEN,    200, "mcp2", "/sse", "", None),
    ("GET",  "/mcp/fs/ping",            MCP_TOKEN,    200, "mcp2", "/ping", "", None),
    ("POST", "/mcp/fs/messages?sessionId=zz", MCP_TOKEN, 200, "mcp2", "/messages", "sessionId=zz", None),
    # generic http service: /svc/echo strips, /raw/echo does not
    ("GET",  "/svc/echo/abc?x=1",       None,         200, "echo-svc", "/abc", "x=1", None),
    ("GET",  "/svc/echo/deep/path",     None,         200, "echo-svc", "/deep/path", "", None),
    ("GET",  "/svc/echo",               None,         200, "echo-svc", "/", "", None),
    ("GET",  "/raw/echo/abc",           None,         200, "echo-svc", "/raw/echo/abc", "", None),
    ("GET",  "/raw/echo",               None,         200, "echo-svc", "/raw/echo", "", None),
    # legacy SSE compat at the origin
    ("GET",  "/sse",                    None,         200, "mcp1", "/sse", "", None),
    ("POST", "/messages?sessionId=orig", None,        200, "mcp1", "/messages", "sessionId=orig", None),
]
for method, path, token, status, upstream, upath, query, handler in cases:
    code, _, body = req(method, path, token)
    label = f"{method} {path}" + (f"  [{token}]" if token else "")
    check(f"{label} -> {status}", code, status)
    if upstream:
        j = jget(body)
        check(f"{label} -> {upstream}{upath}",
              (j.get("name"), j.get("path"), j.get("query"), j.get("handler")),
              (upstream, upath, query, handler))

# the WebUI (catch-all at "/") + everything else still routing correctly
code, _, body = req("GET", "/")
check("GET / -> WebUI (catch-all HTML)", (code, "webui-html" in body), (200, True))
code, _, body = req("GET", "/index.html")
check("GET /index.html -> WebUI", (code, "webui-html" in body), (200, True))
code, _, body = req("GET", "/_app/immutable/entry/app.js")
check("GET /_app/... -> WebUI asset", (code, "app.js" in body), (200, True))
code, _, body = req("GET", "/build.json")
check("GET /build.json -> WebUI asset (PWA no-cache list)", (code, "build.json" in body), (200, True))
check("catch-all doesn't swallow /mcp", jget(req("POST", "/mcp")[2]).get("name"), "mcp1")
check("catch-all doesn't swallow /mcp/fs", jget(req("POST", "/mcp/fs", MCP_TOKEN)[2]).get("name"), "mcp2")
check("catch-all doesn't swallow /v1/models", jget(req("GET", "/v1/models")[2]).get("path"), "/v1/models")
check("catch-all doesn't swallow /health", jget(req("GET", "/health")[2]).get("status"), "ok")
check("UI ungated while /v1 gated", (req("GET", "/")[0], req("POST", "/v1/chat/completions")[0]), (200, 401))
# the UI's own API calls: /v1/chat/completions + the /v1-only stream/control routes
for path in ("/v1/chat/completions", "/v1/chat/completions/control", "/v1/streams/lookup"):
    j = jget(req("POST", path, LLAMA_TOKEN, body={})[2])
    check(f"WebUI call POST {path} reaches llama intact", j.get("path"), path)

# the stream the WebUI reads its tokens from (GET /v1/stream is SSE, /v1-only)
t0, stamps = time.time(), []
r = urllib.request.Request(f"http://127.0.0.1:{PUBLIC}/v1/stream?conv_id=1",
                           headers={"Authorization": f"Bearer {LLAMA_TOKEN}",
                                    "Accept": "text/event-stream"})
with urllib.request.urlopen(r, timeout=20) as resp:
    for line in resp:
        if line.strip().startswith(b"data:"):
            stamps.append(round(time.time() - t0, 2))
check("GET /v1/stream streams (not 404'd by a stripped prefix)",
      (len(stamps), resp.status, stamps[0] < 0.5, stamps[-1] > 0.6), (3, 200, True, True))

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
check("state.json keeps CELL B launch commands",
      sorted((s["name"], bool(s.get("command"))) for s in st["mcp"]),
      [("filesystem", True), ("mock_mcp_stdio", True)])
provider_entry = _normalise([ck.mcp("/mcp/provider-check", 8894, provider="websearch")])[0]
check("normalisation retains the websearch provider for CELL B",
      provider_entry.get("provider"), "websearch")
check("routing-only service table excludes MCP launch settings",
      any("command" in s or "env" in s for s in st["services"] if s["kind"] == "mcp"),
      False)
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
print("\n########## CELL B: ck.mcps() ##########")
# node 20 is what's in this sandbox; a Kaggle image gets node 22 downloaded.
# Use the no-argument form here to exercise CELL B's readback of CELL A state.
node_paths = {"node": NODE, "npm": shutil.which("npm"), "npx": shutil.which("npx")}
mcp_module = importlib.import_module("cellkit.mcps")
original_ensure_node = mcp_module.ensure_node
mcp_module.ensure_node = lambda **kw: node_paths  # noqa: ARG005  (test stub)
try:
    run = ck.mcps()
finally:
    mcp_module.ensure_node = original_ensure_node

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
check("llama /v1 is an identity mount (prefix kept, no rewrite)",
      ("location ~ ^/v1(/.*)?$ {" in conf, "rewrite ^/v1" in conf), (True, False))
check("public paths tolerate a query string",
      conf.count("~*^(\\/health|\\/props)/?(\\?.*)?$ 1;") +
      conf.count("~*^(\\/v1\\/models)/?(\\?.*)?$ 1;"), 2)

# ------------------------------------------------- "the UI is missing" report
# a llama-server with --no-ui answers 404 for / by itself: the gateway has to
# say so instead of looking broken. No nginx needed -- it is an origin probe.
no_ui = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_llama_server.py"),
                          str(NO_UI), "--no-ui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
time.sleep(1)
try:
    from cellkit.gateway import origin_note
    code, note = origin_note({"port": NO_UI, "routes": ["/"]})
    check("origin probe: --no-ui llama explains itself",
          (code, "no UI at /" in note), (404, True))
    check("origin probe: a llama with a UI just reports 200",
          origin_note({"port": LLAMA, "routes": ["/"]})[0], 200)
    check("origin probe: a dead port is not a 404",
          origin_note({"port": 8097, "routes": ["/"]})[0], 0)
finally:
    no_ui.kill()

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
