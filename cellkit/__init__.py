"""cellkit -- tiny helpers that give a Kaggle (or any Linux) notebook ONE public
port for a pile of services: llama-server's WebUI + /v1, and N MCP servers.

    import cellkit as ck

    MCPS = [ck.mcp("/mcp", 8891, provider="websearch")]      # one dict, both cells

    ck.nginx(port=8890, llama=8080, services=MCPS)   # CELL A
    ck.mcps(MCPS)                                    # CELL B

Everything else (nginx config, mcp-proxy processes, auth tokens, rewrites,
health checks) happens inside. Extra handles: ck.status(), ck.stop().
"""
from .gateway import (DEFAULT_DIR, exact, find_nginx, http, llama, nginx, state, status,
                      stop_nginx, svc, webui)
from .mcps import ensure_node, mcp, mcps, running, stop_mcps
from .util import http_probe, kill_port, port_open, pids_on_port, sh, wait_port

__version__ = "0.1.0"
__all__ = ["nginx", "mcps", "svc", "http", "exact", "webui", "llama", "mcp", "status",
           "stop", "state", "running", "port_open", "http_probe", "kill_port", "wait_port",
           "ensure_node", "find_nginx", "DEFAULT_DIR", "__version__"]


def stop(servers=None, directory=DEFAULT_DIR):
    """Stop nginx and the MCP servers. Never touches llama-server."""
    stop_mcps(servers, directory)
    stop_nginx(directory)
