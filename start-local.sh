#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
. "$project_dir/deploy/ensure-docker.sh"
ensure_docker
image=${PREDICTION_AGENT_IMAGE:-ghcr.io/xnetsc/prediction-market-agent:latest}
# Pull through the registry preflight when a host interpreter is available, so a machine that can
# only reach the registry through a proxy still works without touching Docker's own configuration.
python_bin=''
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then python_bin=$candidate; break; fi
done
if [ -n "$python_bin" ]; then
    "$python_bin" "$project_dir/deploy/registry-pull.py" "$image"
else
    echo 'No python3 on PATH; pulling directly without the registry preflight.' >&2
    docker_run compose -f "$project_dir/compose.yaml" pull
fi
sh "$project_dir/deploy/prepare-host-proxy.sh" "$image"
docker_run compose -f "$project_dir/compose.yaml" up -d --no-build --wait --wait-timeout 180
robot=$(docker_run compose -f "$project_dir/compose.yaml" ps -q robot)
case "$robot" in ''|*[!a-f0-9]*) echo 'Cannot identify the running robot container' >&2; exit 1;; esac
sh "$project_dir/deploy/prepare-host-proxy.sh" "$image" --attach-container "$robot"
mkdir -p "$project_dir/runtime-data"
callback_pid="$project_dir/runtime-data/local-callbacks-$robot.pid"
if [ ! -f "$callback_pid" ] || ! kill -0 "$(cat "$callback_pid")" 2>/dev/null; then
    callback_ready="$project_dir/runtime-data/local-callbacks-$robot.ready"
    : > "$callback_ready"
    nohup sh "$project_dir/deploy/local-callbacks.sh" "$robot" 8765 "$callback_ready" \
        > "$project_dir/runtime-data/local-callbacks.log" 2>&1 < /dev/null &
    echo "$!" > "$callback_pid"
    callback_attempt=0
    until [ -s "$callback_ready" ]; do
        callback_attempt=$((callback_attempt + 1))
        if [ "$callback_attempt" -ge 20 ] || ! kill -0 "$(cat "$callback_pid")" 2>/dev/null; then
            echo 'Local callback forwarding failed to start. See runtime-data/local-callbacks.log' >&2
            exit 1
        fi
        sleep 1
    done
fi
echo "Robot is ready: http://127.0.0.1:${PREDICTION_AGENT_PORT:-8765}"
echo 'Local client callback forwarding is managed automatically. Log: runtime-data/local-callbacks.log'
