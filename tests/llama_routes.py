#!/usr/bin/env python3
"""A faithful-enough `llama-server` route table, for the fake llama upstream.

Copied from llama.cpp `tools/server/server.cpp` (the `ctx_http.get/post/del` block),
the embedded-UI paths from `tools/server/server-http.cpp`, and the paths the UI
itself calls from `tools/ui/src/lib/constants/api-endpoints.constants.ts`. The
point is that the fake answers 404 for paths llama does NOT serve, so a test
can tell an identity mount from a stripping one -- a plain echo server accepts
whatever nginx sends it and therefore passes either way.

Keep it in sync by eye when bumping llama.cpp; nothing here is load-bearing
beyond "which paths exist" and "which handler answers them".
"""
import re

# path -> (handler name, http methods). "" methods = any.
ROUTES = {
    # health / introspection
    "/health": ("get_health", {"GET"}),
    "/v1/health": ("get_health", {"GET"}),
    "/metrics": ("get_metrics", {"GET"}),
    "/props": ("get_props", {"GET", "POST"}),
    "/slots": ("get_slots", {"GET"}),
    "/lora-adapters": ("lora_adapters", {"GET", "POST"}),
    "/tokenize": ("post_tokenize", {"POST"}),
    "/detokenize": ("post_detokenize", {"POST"}),
    "/apply-template": ("post_apply_template", {"POST"}),

    # model management (multi-model mode) + the UI's file tools. Note llama answers
    # this path for both GET (list) and POST (download), so a gateway that rewrites
    # /v1/models to /models does *not* 404 -- it just hits a different handler.
    "/models": ("post_router_models", {"GET", "POST", "DELETE"}),
    "/models/load": ("post_router_models_load", {"POST"}),
    "/models/unload": ("post_router_models_unload", {"POST"}),
    "/models/sse": ("get_router_models_sse", {"GET"}),
    "/tools": ("tools", {"GET", "POST"}),
    "/cors-proxy": ("cors_proxy", {"GET", "POST"}),

    # completions -- note the legacy (non-OpenAI) handlers live without /v1
    "/completion": ("post_completions", {"POST"}),            # legacy
    "/completions": ("post_completions", {"POST"}),           # legacy
    "/v1/completions": ("post_completions_oai", {"POST"}),    # OpenAI schema
    "/chat/completions": ("post_chat_completions", {"POST"}),
    "/v1/chat/completions": ("post_chat_completions", {"POST"}),
    "/v1/chat/completions/control": ("post_control", {"POST"}),
    "/infill": ("post_infill", {"POST"}),
    "/embedding": ("post_embeddings", {"POST"}),              # legacy
    "/embeddings": ("post_embeddings", {"POST"}),             # legacy
    "/v1/embeddings": ("post_embeddings_oai", {"POST"}),      # OpenAI schema
    "/rerank": ("post_rerank", {"POST"}),
    "/reranking": ("post_rerank", {"POST"}),
    "/v1/rerank": ("post_rerank", {"POST"}),
    "/v1/reranking": ("post_rerank", {"POST"}),
    "/responses": ("post_responses_oai", {"POST"}),
    "/v1/responses": ("post_responses_oai", {"POST"}),
    "/audio/transcriptions": ("post_transcriptions_oai", {"POST"}),
    "/v1/audio/transcriptions": ("post_transcriptions_oai", {"POST"}),
    "/v1/messages": ("post_anthropic_messages", {"POST"}),    # no bare alias
    "/v1/messages/count_tokens": ("post_anthropic_count_tokens", {"POST"}),
    "/v1/systemone": ("post_systemone", {"POST"}),            # no bare alias
    "/chat/completions/input_tokens": ("post_chat_completions_tok", {"POST"}),
    "/v1/chat/completions/input_tokens": ("post_chat_completions_tok", {"POST"}),
    "/responses/input_tokens": ("post_responses_tok_oai", {"POST"}),
    "/v1/responses/input_tokens": ("post_responses_tok_oai", {"POST"}),

    # OpenAI model list -- same handler as "/models", under a /v1-only method set
    "/v1/models": ("post_router_models", {"GET"}),

    # resumable stream API: /v1-only, and the WebUI reads its tokens over it
    "/v1/stream": ("stream_get", {"GET", "DELETE"}),
    "/v1/streams/lookup": ("streams_lookup", {"POST"}),
}

# POST /slots/<id>: a pattern route, no bare equivalent

PATTERNS = [(re.compile(r"^/slots/(\d+)$"), "post_slot"),
            (re.compile(r"^/v1/slots/(\d+)$"), "post_slot")]

# the embedded WebUI: "/" + every asset name (tools/ui builds _app/..., PWA files)
UI_INDEX = {"/", "/index.html"}
UI_PREFIXES = ("/_app/", "/assets/", "/fonts/", "/images/", "/icons/")
UI_FILES = {"/favicon.ico", "/robots.txt", "/sw.js", "/manifest.webmanifest",
            "/build.json", "/_app/version.json"}
UI_ALL = UI_INDEX | UI_FILES


def is_ui_asset(path):
    """True for "/" and every embedded asset name the WebUI loads."""
    return path in UI_ALL or path.startswith(UI_PREFIXES)


def resolve(method, path):
    """(status, kind, payload) | None -> None means 'this llama has no such route'.

    kind: "json" (echo payload), "html"/"js" (a body string) or "sse" (stream).
    """
    if is_ui_asset(path):
        if path in ("/", "/index.html"):
            return 200, "html", "<html><title>llama-server(fake)</title>webui-html</html>"
        return 200, "js", f"/* llama-server(fake) asset {path} */"

    hit = ROUTES.get(path)
    if hit is None:
        for rx, name in PATTERNS:
            if rx.match(path):
                hit = (name, None)
                break
    if hit is None:
        return None
    handler, methods = hit
    if methods and method not in methods:
        return 405, "json", {"error": {"message": f"{method} not allowed on {path}",
                                       "type": "method_not_allowed", "code": 405},
                             "handler": handler, "path": path}
    if method == "GET" and path == "/v1/stream":
        return 200, "sse", None
    payload = {"handler": handler, "path": path, "method": method}
    if handler == "get_health":
        payload["status"] = "ok"                    # what the WebUI polls to know it is up
    return 200, "json", payload
