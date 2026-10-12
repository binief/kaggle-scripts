#!/bin/bash
# detached so the suite's own pkill patterns can't match the calling shell
cd "$(dirname "$0")/.."
setsid nohup python3 tests/run_tests.py > /tmp/cellkit_test.txt 2>&1 &
echo "started pid $!"
