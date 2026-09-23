#!/usr/bin/env bash
# Stop whatever is on the port, wait for it to actually let go, start again, follow the log.
# Shutting down closes a browser, which takes a moment: starting before that is done fails with
# EADDRINUSE against a service that is on its way out.
set -uo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8899}"
for pid in $(lsof -ti ":$PORT" 2>/dev/null); do kill "$pid" 2>/dev/null; done
for _ in $(seq 1 30); do lsof -ti ":$PORT" >/dev/null 2>&1 || break; sleep 1; done
for pid in $(lsof -ti ":$PORT" 2>/dev/null); do kill -KILL "$pid" 2>/dev/null; done
sleep 1
LAYA_TRACE="${LAYA_TRACE:-}" nohup node server.mjs --port "$PORT" > /tmp/laya.log 2>&1 &
echo "已启动，日志 /tmp/laya.log"
