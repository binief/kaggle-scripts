# cellkit

One public port for everything in a GPU notebook: llama-server's WebUI + `/v1`, plus **N MCP
servers** — from cells that stay five lines long.

```python
# CELL A -- the gateway
import subprocess, sys
subprocess.run("git -C /kaggle/working/cellkit pull -q 2>/dev/null || git clone -q "
               "https://github.com/binief/kaggle-scripts.git /kaggle/working/cellkit", shell=True)
sys.path.insert(0, "/kaggle/working/cellkit")
import cellkit as ck

MCPS = [ck.mcp("/mcp", 8891, provider="websearch")]
ck.nginx(port=8890, llama=8080, token="auto", services=MCPS)
```

```python
# CELL B -- the MCP servers (same list; read back from CELL A's state)
import subprocess, sys
subprocess.run("git -C /kaggle/working/cellkit pull -q 2>/dev/null || git clone -q "
               "https://github.com/binief/kaggle-scripts.git /kaggle/working/cellkit", shell=True)
sys.path.insert(0, "/kaggle/working/cellkit")
import cellkit as ck
ck.mcps()
```

Then `ngrok http 8890` and:

| what | url |
|---|---|
| llama WebUI | `https://<tunnel>/` |
| OpenAI API | `https://<tunnel>/v1` (bearer token printed by CELL A) |
| MCP (http transport) | `https://<tunnel>/mcp` |
| gateway map (JSON) | `http://127.0.0.1:8890/_gateway` |

llama-server itself stays in your own cell on `127.0.0.1:8080`. CELL A never starts or stops it.

## The API

```python
ck.nginx(port=8890, services=[...], llama=8080, token="auto", sse_compat=True)
ck.mcps(servers=None)          # None -> the MCPs mounted in CELL A's state
ck.status()                    # table of routes: up/down, port, auth
ck.stop()                      # stop nginx + MCP servers (llama untouched)
```

Service entries (one dict is enough — the *same* dict is used by both cells):

```python
ck.mcp("/mcp/fs", 8892, npx="@modelcontextprotocol/server-filesystem",
       args=["/kaggle/working"], stateless=True, token="auto")
ck.http("/svc/echo", 9000)                 # prefix mount: /svc/echo/x -> /x
ck.http("/hook", 9001, strip_prefix=False) # keep the prefix
ck.exact("/health", 8080)                  # identity mount
ck.webui(8080)                             # catch-all at "/" (SPAs like llama's UI)
ck.llama(8080, token="auto")               # -> [webui, /v1, ops routes]  (llama= does this)
```

Entry keys: `name, route(s), port, kind, mode, strip_prefix, root, token, public_paths` for
routing, plus `command / npx+args / provider / env / cwd / proxy_args / enabled` for launching.
`token`: `None` | `"auto"` | `"literal"`. `public_paths` stay reachable without the token
(`/v1/models`, `/health`, `/props` — the WebUI needs those).

MCP paths are mounted gateway-style, so any client works:

```
https://<tunnel>/mcp          -> backend /mcp       (streamable HTTP)
https://<tunnel>/mcp/sse      -> backend /sse       (legacy HTTP+SSE)
https://<tunnel>/mcp/messages -> backend /messages
https://<tunnel>/mcp/ping     -> backend /ping
```

Nested mounts are ordered longest-first, so `/mcp/fs` never gets swallowed by `/mcp`.

## Adding MCPs

Add one dict to `MCPS` in CELL A and run both cells — nothing else:

```python
MCPS = [
    ck.mcp("/mcp",    8891, provider="websearch"),                  # clone+build+proxy: automatic
    ck.mcp("/mcp/fs", 8892, npx="@modelcontextprotocol/server-filesystem",
           args=["/kaggle/working"], stateless=True),
    ck.mcp("/mcp/fetch", 8893, npx="mcp-server-fetch"),
]
```

* `provider="websearch"` builds [mrkrsl/web-search-mcp](https://github.com/mrkrsl/web-search-mcp)
  (git clone → npm install → playwright chromium → tsc) and points the entry at `dist/index.js`.
  Re-runs skip whatever already exists.
* `npx="pkg"` runs any published MCP server via `npx -y pkg …`.
* `command=[...]` is the escape hatch (any stdio MCP, e.g. `["node", "/path/server.js"]`).
* `stateless=True` adds `--stateless` to mcp-proxy (use it if a client doesn't resend
  `Mcp-Session-Id`).
* Names are auto-derived (`provider` → npx package → command basename → route); pass `name=`
  to choose your own.

## What happens under the hood

* CELL A renders a fresh `nginx.conf` from your list, runs `nginx -t`, then starts nginx on the
  public port (installing nginx if the image lacks it). Auth is a per-service
  `Authorization: Bearer …` map; 401s happen before the backend is touched.
* CELL B downloads Node 22 if needed, then runs one `mcp-proxy --port … -- <your stdio server>`
  per MCP, and health-checks each one like a client (`/ping` + a real `initialize`) — so a port
  that answers but whose stdio child is dead is reported as failed, with the log tail.
* Cleanup uses `ss` → `lsof` → a `/proc` scan (so it works on images without `fuser`), and
  CELL A/CELL B only ever touch their own processes — never your llama-server.
* Streaming is left unbuffered (`proxy_buffering off`, 24 h timeouts), verified by tests.

Files land in `/kaggle/working/mcp_gateway/`: `nginx.conf`, `state.json`, `mcp_running.json`,
`nginx_access.log`, `mcp-<name>.log`.

## Gotchas

* Only one service may own `/` (that's the WebUI catch-all) and it must be its only route.
* `/_gateway` is reserved for the discovery document.
* The WebUI is left ungated on purpose (it's a static page) — its chat calls `/v1`, which *is*
  gated. Paste the printed token into the UI (Settings → General → API Key), or set
  `token=None` on `llama_api`. If you also pass llama-server an `--api-key`, that's a second gate
  behind nginx: pick one.
* Legacy SSE (`/sse` + origin-level `/messages`) can only belong to one MCP: `sse_compat=True`
  gives it to the first MCP in your list. Modern streamable-HTTP clients don't need it.

## Tests

```bash
python3 tests/run_tests.py
```

Runs both cells for real (only the upstreams are mocked) and drives everything through the one
public port — **73 checks, all passing**.

## Install (optional)

```bash
pip install -e .        # just puts `cellkit` on sys.path; no dependencies
```

MIT.
