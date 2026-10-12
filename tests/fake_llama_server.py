#!/usr/bin/env python3
"""Fake `llama-server` binary: ignores llama flags, serves the mock upstream.

Only used by the test harness so CELL A can run without a real model.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_upstream import serve  # noqa: E402

port = 8080
if "--port" in sys.argv:
    port = int(sys.argv[sys.argv.index("--port") + 1])
serve(port, "llama-server(fake)")
