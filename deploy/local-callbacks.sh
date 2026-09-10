#!/bin/sh
# Kept on the host: the application container never receives the Docker socket.
set -eu
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$deployment_dir/ensure-docker.sh"
if ! docker info >/dev/null 2>&1; then
    docker_run() { sudo -n docker "$@"; }
fi
robot=$(docker_run inspect --format '{{.Id}}' "$1")
case "$robot" in ''|*[!a-f0-9]*) echo 'Invalid container ID' >&2; exit 1;; esac
image=$(docker_run inspect --format '{{.Image}}' "$robot")
network=$(docker_run inspect --format '{{range $name,$net := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$robot" | head -n 1)
target=$(docker_run inspect --format "{{(index .NetworkSettings.Networks \"$network\").IPAddress}}" "$robot")
prefix="prediction-callback-$(printf '%s' "$robot" | cut -c 1-12)"
label="org.prediction-agent.callback-owner=$robot"
transport="$deployment_dir/local-callbacks.py"
ready_file=${3:-}
cleanup() {
    for child in $(docker_run ps -aq --filter "label=$label"); do
        docker_run rm -f "$child" >/dev/null || true
    done
}
trap cleanup EXIT
trap 'exit 0' INT TERM
# The watcher emits only validated ADD/REMOVE, a port, and its remaining lifetime.
docker_run exec -i "$robot" python -u - watch --api-port "${2:-8765}" < "$transport" |
while read -r action port lifetime; do
    if [ "$action $port $lifetime" = 'READY 0 0' ]; then
        if [ -n "$ready_file" ]; then echo ready > "$ready_file"; fi
        continue
    fi
    case "$port:$lifetime" in *[!0-9:]*|'') echo 'Invalid callback event' >&2; exit 1;; esac
    [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || exit 1
    name="$prefix-$port"
    case "$action" in
        ADD)
            # An explicit existing mapping already belongs to the robot.
            if [ -n "$(docker_run port "$robot" "$port/tcp" 2>/dev/null || true)" ]; then continue; fi
            if ! docker_run run -d --rm --name "$name" --label "$label" \
                --network "$network" -p "127.0.0.1:$port:$port" \
                --read-only --cap-drop ALL --security-opt no-new-privileges \
                --memory 64m --pids-limit 32 --entrypoint python \
                --mount "type=bind,source=$transport,target=/transport.py,readonly" \
                "$image" /transport.py tunnel --target "$target" --port "$port" --lifetime "$lifetime" >/dev/null; then
                echo "Callback port $port could not be published; check for a local port conflict and retry login." >&2
            else
                echo "Published local callback port $port"
            fi
            ;;
        REMOVE)
            if [ "$(docker_run inspect --format '{{index .Config.Labels "org.prediction-agent.callback-owner"}}' "$name" 2>/dev/null || true)" = "$robot" ]; then
                docker_run stop -t 5 "$name" >/dev/null 2>&1 || docker_run rm "$name" >/dev/null || true
            fi
            ;;
        *) echo 'Invalid callback action' >&2; exit 1;;
    esac
done
