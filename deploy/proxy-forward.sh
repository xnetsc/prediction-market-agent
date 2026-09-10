#!/bin/sh
set -eu
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Proxy forwarding requires Python 3.9+. Install it and rerun the launcher.' >&2
    exit 1
fi
exec python3 "$(dirname "$0")/proxy-forward.py" "$@"
