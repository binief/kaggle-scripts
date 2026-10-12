"""The nginx gateway: one public port, N services.

    ck.nginx(port=8890, llama=8080, token="auto", services=MCPS)

Every service is a dict (build them with ck.http / ck.exact / ck.mcp / ck.webui
or plain dicts). This module only reads route/port/kind/mode/token from them --
the same dicts CELL B feeds to ck.mcps().
"""
import json
import os
import re
import time

from .util import (kill_port, port_open, regex_escape, resolve_token, sh, spawn, tail, wait_port)

DEFAULT_DIR = "/kaggle/working/mcp_gateway"
NGINX_CANDIDATES = ["nginx", "/usr/sbin/nginx", "/usr/local/sbin/nginx",
                    "/usr/local/nginx/sbin/nginx", "/opt/conda/bin/nginx", "/sbin/nginx"]


# ---------------------------------------------------------------- service dicts
def svc(route, port, name=None, routes=None, kind="http", mode="prefix",
        strip_prefix=True, root="/", token=None, public_paths=(), **extra):
    """One service. `route` may be a path, a list of paths, or a plain name."""
    routes = routes or (route if isinstance(route, (list, tuple)) else [route])
    out = {"name": name or re.sub(r"\W", "_", str(routes[0]).strip("/") or "root") or "root",
           "routes": list(routes), "port": int(port), "kind": kind, "mode": mode,
           "strip_prefix": strip_prefix, "root": root, "token": token,
           "public_paths": list(public_paths)}
    out.update(extra)
    return out


def http(route, port, **kw):
    """Prefix mount: /v1/chat/x -> backend /chat/x (bare /v1 -> backend /)."""
    return svc(route, port, **kw)


def exact(route, port, **kw):
    """Identity mount: /health -> backend /health."""
    kw["mode"] = "exact"
    return svc(route, port, **kw)


def mcp(route, port, token=None, **kw):
    """stdio MCP behind mcp-proxy: /mcp -> /mcp, /mcp/sse -> /sse, ..."""
    kw["kind"] = "mcp"
    return svc(route, port, token=token, **kw)


def webui(port, name="llama_ui", **kw):
    """Catch-all at "/" -- llama-server's built-in WebUI (an SPA with absolute assets)."""
    return svc("/", port, name=name, token=kw.pop("token", None), **kw)


def llama(port=8080, token="auto", public_paths=("/health", "/props", "/v1/models")):
    """The three entries llama-server needs: WebUI catch-all + /v1 + ops routes."""
    def _pub(*want):
        return [p for p in want if p in public_paths]

    return [
        webui(port),
        http("/v1", port, name="llama_api", token=token, public_paths=_pub("/v1/models")),
        exact(["/health", "/props", "/metrics", "/slots", "/tokenize", "/detokenize"], port,
              name="llama_ops", token=token, public_paths=_pub("/health", "/props")),
    ]


# ---------------------------------------------------------------- nginx binary
def find_nginx(candidates=None, install=True):
    cands = list(candidates or NGINX_CANDIDATES)

    def scan():
        for c in cands:
            path = c if os.path.isabs(c) else sh(f"command -v {c}", capture_output=True,
                                                 text=True).stdout.strip()
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return ""

    if (found := scan()) or not install:
        return found

    def apt(args):
        pre = "export DEBIAN_FRONTEND=noninteractive; "
        r = sh(pre + f"apt-get update -qq && apt-get install -y -qq {args}",
               capture_output=True, text=True)
        if r.returncode and hasattr(os, "geteuid") and os.geteuid() != 0:
            r = sh(pre + f"sudo apt-get update -qq && sudo apt-get install -y -qq {args}",
                   capture_output=True, text=True)
        return r

    print("   nginx not found -> installing it")
    res = apt("nginx")
    if not (found := scan()):
        res = apt("--reinstall nginx")
        found = scan()
    if not found:
        lines = (res.stderr or res.stdout or "").strip().splitlines()[-6:]
        print("   apt-get could not provide nginx: " + " | ".join(lines))
        print("   fix: !apt-get update && !apt-get install -y nginx"
              "  (or pass nginx_bin='/usr/sbin/nginx')")
    return found


# ---------------------------------------------------------------- config render
def _normalise(services):
    out = []
    for s in services:
        if not s.get("routes"):
            raise SystemExit(f"service {s.get('name')!r} has no routes")
        routes = [str(r) if str(r).startswith("/") else "/" + str(r) for r in s["routes"]]
        routes = [r.rstrip("/") or "/" for r in routes]
        out.append({
            "name": str(s.get("name") or routes[0].strip("/") or "root"),
            "routes": routes,
            "port": int(s["port"]),
            "kind": s.get("kind", "http"),
            "mode": s.get("mode", "prefix"),
            "strip_prefix": bool(s.get("strip_prefix", True)),
            "root": str(s.get("root") or "/"),
            "upstream_path": (str(s.get("upstream_path") or "/mcp").rstrip("/") or "/mcp"),
            "token": resolve_token(s.get("token")),
            "public_paths": [str(p) for p in (s.get("public_paths") or [])],
            "alias": "svc_" + re.sub(r"\W", "_", str(s.get("name") or "root")),
        })
    out.sort(key=lambda e: max(len(r) for r in e["routes"]), reverse=True)  # /mcp/fs first
    return out


def _validate(entries, reserved=("/_gateway",)):
    seen = {}
    for e in entries:
        if e["kind"] not in ("http", "mcp"):
            raise SystemExit(f"SERVICES: [{e['name']}] kind must be 'http' or 'mcp'")
        if e["mode"] not in ("prefix", "exact") or (e["kind"] == "mcp" and e["mode"] != "prefix"):
            raise SystemExit(f"SERVICES: [{e['name']}] bad mode {e['mode']!r}")
        for r in e["routes"]:
            if not re.fullmatch(r"/|/[A-Za-z0-9._~/-]+", r):
                raise SystemExit(f"SERVICES: [{e['name']}] unsafe route {r!r}")
            if r in seen:
                raise SystemExit(f"SERVICES: route {r} claimed by {seen[r]} and {e['name']}")
            if r in reserved:
                raise SystemExit(f"SERVICES: route {r} is reserved by the gateway")
            seen[r] = e["name"]
    catch = [e for e in entries if "/" in e["routes"]]
    if len(catch) > 1:
        raise SystemExit("SERVICES: only one service may own '/': "
                         + ", ".join(e["name"] for e in catch))
    if catch and len(catch[0]["routes"]) > 1:
        raise SystemExit(f"SERVICES: [{catch[0]['name']}] owns '/' so it needs that route alone")
    return catch


def _render(entries, port, sse_compat, directory):
    def inc(name):
        return os.path.join(directory, name)

    mcp_inc, http_inc = inc("nginx-mcp-common.inc"), inc("nginx-http-common.inc")
    with open(mcp_inc, "w") as f:
        f.write("""# MCP proxy headers (streamable HTTP + legacy SSE)
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Connection "";
proxy_set_header Cache-Control "no-cache";
proxy_set_header Accept $http_accept;
proxy_set_header Content-Type $content_type;
proxy_set_header mcp-session-id $http_mcp_session_id;
proxy_set_header mcp-protocol-version $http_mcp_protocol_version;
proxy_pass_header mcp-session-id;
proxy_pass_header Mcp-Session-Id;
proxy_read_timeout 86400s;
proxy_send_timeout 86400s;
chunked_transfer_encoding on;
""")
    with open(http_inc, "w") as f:
        f.write("""# plain HTTP service headers
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_read_timeout 86400s;
proxy_send_timeout 86400s;
""")

    maps = []
    for e in entries:
        if not e["token"]:
            continue
        maps.append(f"    map $http_authorization ${e['alias']}_ok {{\n"
                    f"        default 0;\n"
                    f'        "~*^Bearer[[:space:]]+{regex_escape(e["token"])}$" 1;\n'
                    f"    }}")
        if e["public_paths"]:
            pub = "|".join(regex_escape(p) for p in e["public_paths"])
            maps.append(f"    map $request_uri ${e['alias']}_public {{\n"
                        f"        default 0;\n"
                        f"        ~*^({pub})$ 1;\n"
                        f"    }}\n"
                        f"    map \"${e['alias']}_ok${e['alias']}_public\" ${e['alias']}_deny {{\n"
                        f"        default 0;\n"
                        f'        "00"    1;\n'
                        f"    }}")

    def gate(e):
        if not e["token"]:
            return ""
        var = f"{e['alias']}_deny" if e["public_paths"] else f"{e['alias']}_ok"
        cmp_ = "= 1" if e["public_paths"] else "= 0"
        return f"        if (${var} {cmp_}) {{ return 401; }}\n"

    upstreams = [f"    upstream {e['alias']} {{ server 127.0.0.1:{e['port']}; keepalive 32; }}"
                 for e in entries]

    locations, catch_all = [], []
    for e in entries:
        g, inc_file = gate(e), (mcp_inc if e["kind"] == "mcp" else http_inc)
        for route in e["routes"]:
            rx = re.escape(route)
            if e["kind"] == "mcp":
                up = e["upstream_path"]
                locations.append(f"""
        # -- {e['name']} : {route}  ->  127.0.0.1:{e['port']}{up}   [mcp]
        location = {route} {{
{g}        proxy_pass http://{e['alias']}{up};
        include {inc_file};
        }}""".rstrip())
                locations.append(f"""
        location = {route}/ {{
{g}        proxy_pass http://{e['alias']}{up};
        include {inc_file};
        }}""".rstrip())
                if route != "/":
                    locations.append(f"""
        location ~ ^{rx}(/.*)?$ {{
{g}        rewrite ^{rx}(/.*)$ $1 break;
        proxy_pass http://{e['alias']};
        include {inc_file};
        }}""".rstrip())
            elif e["mode"] == "exact":
                locations.append(f"""
        # -- {e['name']} : {route}  ->  127.0.0.1:{e['port']}{route}   [exact]
        location = {route} {{
{g}        proxy_pass http://{e['alias']};
        include {inc_file};
        }}""".rstrip())
            elif route == "/":
                catch_all.append(f"""
        # -- {e['name']} : /  ->  127.0.0.1:{e['port']}   [catch-all, path unchanged]
        location / {{
{g}        proxy_pass http://{e['alias']};
        include {inc_file};
        }}""".rstrip())
            else:
                root = "" if e["root"] == "/" else e["root"]
                locations.append(f"""
        # -- {e['name']} : {route}  ->  127.0.0.1:{e['port']}{root or '/'}   [prefix]
        location = {route} {{
{g}        proxy_pass http://{e['alias']}{root}/;
        include {inc_file};
        }}""".rstrip())
                locations.append(f"""
        location = {route}/ {{
{g}        proxy_pass http://{e['alias']}{root}/;
        include {inc_file};
        }}""".rstrip())
                if e["strip_prefix"]:
                    locations.append(f"""
        location ~ ^{rx}(/.*)$ {{
{g}        rewrite ^{rx}(/.*)$ {root + '$1' if root else '$1'} break;
        proxy_pass http://{e['alias']};
        include {inc_file};
        }}""".rstrip())
                else:
                    locations.append(f"""
        location ~ ^{rx}(/.*)?$ {{
{g}        proxy_pass http://{e['alias']};
        include {inc_file};
        }}""".rstrip())

    if sse_compat:
        e = next((x for x in entries if x["name"] == sse_compat), None)
        if not e:
            raise SystemExit(f"SSE_COMPAT_MCP={sse_compat!r} is not one of the service names")
        locations.append(f"""
        # -- legacy HTTP+SSE compat for [{e['name']}]
        location = /sse {{
            proxy_pass http://{e['alias']}/sse;
            include {mcp_inc};
        }}
        location /messages {{
            rewrite ^/messages(/.*)$ $1 break;
            proxy_pass http://{e['alias']};
            include {mcp_inc};
        }}""".rstrip())

    index_body = json.dumps({
        "gateway": "nginx", "port": port,
        "services": [{"name": e["name"], "routes": e["routes"], "port": e["port"],
                      "kind": e["kind"], "auth": bool(e["token"]),
                      "catch_all": "/" in e["routes"]} for e in entries],
    }, separators=(",", ":")).replace("'", "")
    discovery = f"""
        location = /_gateway {{
            default_type application/json;
            return 200 '{index_body}';
        }}""".rstrip()

    return f"""# generated by cellkit.gateway -- edit your SERVICES list, not this file
worker_processes 1;
error_log {directory}/nginx_error.log warn;
pid {directory}/nginx.pid;
daemon off;

events {{ worker_connections 2048; }}

http {{
    log_format gw '$remote_addr [$time_local] "$request" $status '
                  'up=$upstream_addr rt=$request_time urt=$upstream_response_time';
    access_log {directory}/nginx_access.log gw;

    default_type application/octet-stream;
    client_max_body_size 32m;
    client_body_buffer_size 1m;
    sendfile on;

    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
    proxy_connect_timeout 10s;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;

    map $http_upgrade $connection_upgrade {{ default upgrade; '' ""; }}

{chr(10).join(maps)}
{chr(10).join(upstreams)}

    server {{
        listen 0.0.0.0:{port};
        server_name _;
        charset off;
{chr(10).join(locations)}
{chr(10).join(catch_all)}
{discovery}
    }}
}}
"""


# ---------------------------------------------------------------- public API
def nginx(port=8890, services=(), llama=None, token="auto", sse_compat=None,  # noqa: C901
          directory=DEFAULT_DIR, nginx_bin="", quiet=False):
    """(Re)start nginx on `port` with `services`. Returns the state dict.

    llama=8080            -> adds WebUI catch-all + /v1 + ops routes
    llama=[...dicts...]   -> use those entries instead
    llama=None            -> only the services you pass
    """
    os.makedirs(directory, exist_ok=True)
    services = [dict(s) for s in services]

    if llama is not None:
        entries = llama if isinstance(llama, (list, tuple)) else llama_(int(llama), token)
        services = list(entries) + services

    services = [s for s in services if s.get("enabled", True)]
    if not services:
        raise SystemExit("cellkit.nginx: no services (pass services=[...] or llama=<port>)")

    if sse_compat is True:               # "use the first MCP *I* listed"
        first = next((s for s in services if s.get("kind") == "mcp"), None)
        if not first:
            raise SystemExit("cellkit.nginx: sse_compat=True but no mcp service is mounted")
        sse_compat = str(first.get("name") or
                         (first["routes"] if isinstance(first.get("routes"), (list, tuple))
                          else [first.get("route") or "/mcp"])[0].strip("/"))

    entries = _normalise(services)
    catch = _validate(entries)
    conf = _render(entries, port, sse_compat, directory)
    conf_path = os.path.join(directory, "nginx.conf")
    with open(conf_path, "w") as f:
        f.write(conf)

    nginx_bin = nginx_bin or find_nginx()
    if not nginx_bin:
        raise SystemExit("cellkit.nginx: nginx is not available (see the line above)")
    test = sh([nginx_bin, "-t", "-c", conf_path], capture_output=True, text=True)
    if test.returncode:
        print(conf)
        raise SystemExit("cellkit.nginx: config invalid -> "
                         + (test.stderr or test.stdout).strip().splitlines()[-1])

    sh("pkill -9 -f 'nginx: master'", capture_output=True)
    kill_port(port)
    spawn([nginx_bin, "-c", conf_path], os.path.join(directory, "nginx-fg.log"))
    up = wait_port(port, tries=15)
    if not up:
        tail(os.path.join(directory, "nginx-fg.log"))

    state = {
        "public_port": port, "conf": conf_path, "updated": time.time(),
        "services": entries,
        "mcp": [{**e, "route": e["routes"][0]} for e in entries if e["kind"] == "mcp"],
        "tokens": {e["name"]: e["token"] for e in entries if e["token"]},
    }
    state_path = os.path.join(directory, "state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.chmod(state_path, 0o600)
    globals()["STATUS"] = state

    if not quiet:
        print(f"\n{'=' * 68}\nTUNNEL THIS PORT:  0.0.0.0:{port}   "
              f"(nginx {'ok' if up else 'FAILED'})\n{'-' * 68}")
        for e in entries:
            tag = "mcp" if e["kind"] == "mcp" else ("catch-all" if "/" in e["routes"]
                                                   else e["mode"])
            for r in e["routes"]:
                how = f"127.0.0.1:{e['port']:<6}"
                live = "up  " if port_open(e["port"]) else "down"
                print(f"{r:<22}{how}{live}  {(e['name'] + '  [' + tag + ']') if r == e['routes'][0] else ''}")
        for name, tok in state["tokens"].items():
            print(f"  token [{name}]: Bearer {tok}")
        if catch:
            print(f"  WebUI:  http://127.0.0.1:{port}/   ->  https://<tunnel>/")
        print(f"  map:    http://127.0.0.1:{port}/_gateway   |   ngrok http {port}\n{'=' * 68}")
    return state


llama_ = llama          # internal alias (kept out of the public tables)


def state(directory=DEFAULT_DIR):
    path = os.path.join(directory, "state.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def status(directory=DEFAULT_DIR):
    """Print what is mounted where, and whether each backend is up."""
    st = state(directory)
    if not st:
        print("cellkit: no state yet -- run ck.nginx(...) first")
        return {}
    print(f"gateway on {st['public_port']}  "
          + ("(nginx up)" if port_open(st["public_port"]) else "(nginx DOWN)"))
    for e in st.get("services", []):
        live = "up  " if port_open(e["port"]) else "down"
        print(f"  {','.join(e['routes']):<24}-> 127.0.0.1:{e['port']:<6}{live}"
              f"{' [auth]' if e['token'] else ''}  {e['name']}")
    return st


def stop_nginx(directory=DEFAULT_DIR):
    st = state(directory)
    sh(f"pkill -9 -f {directory}", capture_output=True)
    if st:
        kill_port(st["public_port"])
    print("cellkit: nginx stopped")
