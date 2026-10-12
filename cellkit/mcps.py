"""MCP side: Node + one mcp-proxy per stdio MCP server.

    ck.mcps()              # starts the MCP services from CELL A's state
    ck.mcps(MCPS)          # or an explicit list (same dicts ck.nginx used)

Each entry needs route+port (for the gateway) and how to run the stdio server:
command=[...] or npx="<package>" [+ args=[...]]. `provider="websearch"` builds
mrkrsl/web-search-mcp (clone + npm + playwright + tsc) and uses its dist.
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request

from .gateway import DEFAULT_DIR, state
from .util import first_json, kill_port, port_open, sh, spawn, tail, wait_port

NODE_DIR = "/usr/local/node22"
NODE_VER = "v22.12.0"
PROXY_PKG = "mcp-proxy"
WEBSEARCH_REPO = "/kaggle/working/web-search-mcp"


# ---------------------------------------------------------------- service dicts
def _default_name(route, provider, npx, command):
    if provider:
        return str(provider)
    if npx:
        return npx.split("/")[-1].replace("mcp-server-", "").replace("server-", "")
    if command:
        tail = [str(c) for c in command if not str(c).endswith(("node", "npx"))]
        if tail:
            base = os.path.basename(tail[-1].split()[0])
            return os.path.splitext(base)[0] or "mcp"
    return re.sub(r"\W", "_", str(route).strip("/")) or "mcp"


def mcp(route, port, command=None, cwd=None, env=None, npx=None, args=None,
        provider=None, stateless=False, proxy_args=None, token=None, enabled=True, **extra):
    """An MCP service. Same dict works for ck.nginx(...)."""
    out = {"name": extra.pop("name", None) or _default_name(route, provider, npx, command),
           "routes": [route], "route": route, "port": int(port), "kind": "mcp",
           "token": token, "enabled": enabled, "cwd": cwd, "env": dict(env or {}),
           "command": list(command) if command else None, "npx": npx,
           "args": list(args or []), "provider": provider, "stateless": stateless,
           "proxy_args": list(proxy_args or [])}
    if stateless and "--stateless" not in out["proxy_args"]:
        out["proxy_args"].append("--stateless")
    out.update(extra)
    return out


# ---------------------------------------------------------------- Node + preps
def ensure_node(version=NODE_VER, node_dir=NODE_DIR):
    """Return {"node":..., "npm":..., "npx":...}, installing Node if needed."""
    node = os.path.join(node_dir, "bin/node")
    if not os.path.isfile(node):
        base = f"https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz"
        print(f"   node not found -> downloading {version}")
        sh("apt-get update -qq && apt-get install -y -qq wget xz-utils git curl",
           capture_output=True)
        sh(f"wget -q {base} -O /tmp/node.tar.xz", check=True)
        os.makedirs(node_dir, exist_ok=True)
        sh(f"tar -xf /tmp/node.tar.xz -C {node_dir} --strip-components=1", check=True)
    return {"node": node, "npm": os.path.join(node_dir, "bin/npm"),
            "npx": os.path.join(node_dir, "bin/npx")}


def prepare_websearch(cfg):
    """Clone + build mrkrsl/web-search-mcp and point the entry at its dist."""
    dist = os.path.join(WEBSEARCH_REPO, "dist/index.js")
    if not os.path.isfile(dist):
        npm = os.path.join(NODE_DIR, "bin/npm")
        npx = os.path.join(NODE_DIR, "bin/npx")
        if not os.path.isdir(os.path.join(WEBSEARCH_REPO, ".git")):
            shutil.rmtree(WEBSEARCH_REPO, ignore_errors=True)
            sh(["git", "clone", "--depth", "1",
                "https://github.com/mrkrsl/web-search-mcp.git", WEBSEARCH_REPO], check=True)
        if not os.path.isdir(os.path.join(WEBSEARCH_REPO, "node_modules")):
            sh([npm, "install", "--no-audit", "--no-fund"], cwd=WEBSEARCH_REPO, check=True)
        if not os.path.isfile(os.path.join(WEBSEARCH_REPO, ".pw-chromium-ok")):
            sh([npx, "playwright", "install", "--with-deps", "chromium"], cwd=WEBSEARCH_REPO)
            open(os.path.join(WEBSEARCH_REPO, ".pw-chromium-ok"), "w").write("ok")
        if sh([npm, "run", "build"], cwd=WEBSEARCH_REPO).returncode:
            ts = os.path.join(WEBSEARCH_REPO, "tsconfig.json")
            if os.path.isfile(ts) and "skipLibCheck" not in open(ts).read():
                open(ts, "a").write('\n// cellkit: skipLibCheck\n')
                txt = open(ts).read().replace("{", '{ "skipLibCheck": true,', 1)
                open(ts, "w").write(txt)
            sh([npm, "run", "build"], cwd=WEBSEARCH_REPO)
        if not os.path.isfile(dist):
            return False
    cfg["cwd"] = cfg.get("cwd") or WEBSEARCH_REPO
    cfg["command"] = [os.path.join(NODE_DIR, "bin/node"), dist]
    cfg["env"] = {"BROWSER_HEADLESS": "true", "MAX_BROWSERS": "1", "BROWSER_TYPES": "chromium",
                  "MAX_CONTENT_LENGTH": "12000", "DEFAULT_TIMEOUT": "8000", **cfg["env"]}
    return True


PROVIDERS = {"websearch": prepare_websearch}


# ---------------------------------------------------------------- health checks
def verify(port, timeout=30):
    """(ping_ok, server_info) -- talks to the proxy exactly like a client."""
    ping = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5) as r:
            ping = r.read().decode(errors="replace").strip().lower().startswith("pong")
    except Exception:
        pass
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "cellkit", "version": "1"}}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    info = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = first_json(r.read().decode(errors="replace"))
        si = (payload.get("result") or {}).get("serverInfo") or {}
        info = f"{si.get('name', '')} {si.get('version', '')}".strip() or \
               ("initialize ok" if payload.get("result") else "")
    except Exception as e:
        info = f"initialize failed: {type(e).__name__}"
    return ping, info


def _spawn(cfg, node_paths, directory):
    name, port = cfg["name"], int(cfg["port"])
    log = os.path.join(directory, f"mcp-{name}.log")
    command = cfg.get("command")
    if cfg.get("npx"):
        command = [node_paths["npx"], "-y", cfg["npx"], *cfg.get("args", [])]
    if not command:
        print(f" -  {name}: no command/npx given, skipped")
        return None
    command = [node_paths["node"] if c == "node" else node_paths["npx"] if c == "npx" else c
               for c in command]
    cmd = [node_paths["npx"], "-y", PROXY_PKG, "--port", str(port),
           "--connectionTimeout", "120000", *cfg.get("proxy_args", []), "--", *command]
    env = {**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
    print(f" -> {name}: 127.0.0.1:{port}  (log: {log})")
    proc = spawn(cmd, log, cwd=cfg.get("cwd"), env=env)
    if not wait_port(port, tries=60):
        print(f"    FAIL {name} did not bind {port} -- log tail:")
        tail(log, 30)
        proc.kill()
        return None
    ping, info = verify(port)
    if not (ping and info) or info.startswith("initialize failed"):
        print(f"    FAIL {name} (ping={ping}, init={info or 'none'}) -- log tail:")
        tail(log, 30)
        proc.kill()
        return None
    print(f"    OK  {name}  ping=pong  initialize={info}")
    return {"name": name, "port": port, "route": cfg.get("route"),
            "server_info": info, "pid": proc.pid, "log": log, "token": cfg.get("token") or ""}


# ---------------------------------------------------------------- public API
def mcps(servers=None, directory=DEFAULT_DIR, node_dir=NODE_DIR, public_port=None):
    """Start every MCP in `servers` (or in CELL A's state). Returns the running list."""
    if servers is None:
        st = state(directory)
        servers = st.get("mcp") or []
        public_port = public_port or st.get("public_port")
        if not servers:
            raise SystemExit("cellkit.mcps: nothing to start -- pass servers=[...] "
                             "or run ck.nginx(...) first")
    servers = [dict(s) for s in servers if s.get("enabled", True)]
    if not servers:
        raise SystemExit("cellkit.mcps: every entry is disabled")

    for s in servers:                                  # free our ports before binding
        kill_port(int(s["port"]))
    sh("pkill -9 -f 'mcp-proxy'", capture_output=True)
    time.sleep(1)

    node_paths = ensure_node(node_dir=node_dir)
    print(f"   node {sh([node_paths['node'], '-v'], capture_output=True, text=True).stdout.strip()}"
          f"   starting {len(servers)} MCP server(s)")

    running = []
    for cfg in servers:
        hook = PROVIDERS.get(cfg.get("provider") or "")
        if hook and not hook(cfg):
            print(f" -  {cfg['name']}: build failed, not started")
            continue
        rec = _spawn(cfg, node_paths, directory)
        if rec:
            running.append(rec)

    with open(os.path.join(directory, "mcp_running.json"), "w") as f:
        json.dump({"updated": time.time(), "public_port": public_port, "servers": running},
                  f, indent=2)

    print(f"\n{'=' * 68}")
    for r in running:
        print(f"{r['name']:<14} 127.0.0.1:{r['port']:<6} initialize={r['server_info']}")
    if public_port:
        print("-" * 68)
        for r in running:
            print(f'  {r["name"]:<12} {{"type": "http", "url": '
                  f'"https://<tunnel>{r["route"]}"}}')
    print(f"{'=' * 68}")
    return running


def stop_mcps(servers=None, directory=DEFAULT_DIR):
    if servers is None:
        servers = (state(directory).get("mcp") or [])
    for s in servers:
        kill_port(int(s["port"]))
    sh("pkill -9 -f 'mcp-proxy'", capture_output=True)
    print("cellkit: MCP servers stopped")


def running(directory=DEFAULT_DIR):
    path = os.path.join(directory, "mcp_running.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("servers", [])
    return []
