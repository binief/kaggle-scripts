#!/usr/bin/env python3
"""Fake `llama-server` binary: ignores llama flags, serves the mock upstream.

Only used by the test harness so CELL A can run without a real model. Unlike the
generic mock it answers from llama.cpp's real route table (tests/llama_routes.py),
so a path llama does not serve is a 404 for the fake too. `--no-ui` stands in
for a server started with --no-ui (or built without the embedded WebUI): every
UI asset 404s, the API keeps working.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_upstream import serve          # noqa: E402
from llama_routes import is_ui_asset, resolve as llama_resolve  # noqa: E402


def resolve_factory(no_ui=False):
    def resolve(method, path):
        if no_ui and is_ui_asset(path):
            return None                                   # nothing mounted at "/"
        return llama_resolve(method, path)
    return resolve


def main(argv):
    port = 8080
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    elif len(argv) > 1 and argv[1].isdigit():
        port = int(argv[1])                               # the harness passes it bare
    serve(port, "llama-server(fake)", resolve=resolve_factory("--no-ui" in argv))


if __name__ == "__main__":
    main(sys.argv)
