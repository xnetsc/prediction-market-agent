#!/bin/sh
set -eu
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname "$deployment_dir")
. "$deployment_dir/ensure-docker.sh"
proxy_docker_sudo=false
if ! docker info >/dev/null 2>&1; then
    docker_run() { sudo -n docker "$@"; }
    proxy_docker_sudo=true
fi
image=${1:?Image required}
private_dir="$project_dir/runtime-data/.deployment"
mkdir -p "$private_dir"
chmod 700 "$private_dir"
prepare() {
    test_url=${PREDICTION_AGENT_PROXY_TEST_URL:-https://api.ipify.org?format=json}
    docker_run run --rm -i --user "$(id -u):$(id -g)" --add-host host.proxy.internal:host-gateway --entrypoint python \
      --mount "type=bind,source=$private_dir,target=/deployment" \
      --mount "type=bind,source=$deployment_dir/host-proxy.py,target=/host-proxy.py,readonly" \
      "$image" /host-proxy.py --output /deployment/host-proxy.json --test-url "$test_url" "$@"
}
if [ "${2:-}" = '--attach-container' ]; then prepare "$2" "$3"; exit; fi
capture=$(sh "$deployment_dir/host-proxy.sh" | prepare)
printf '%s\n' "$capture"
case "$capture" in
    *'Forwarding required: yes'*)
        bind=0.0.0.0
        host=host.docker.internal
        if [ "$(uname -s)" = Linux ]; then
            bind=$(docker_run network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
            host=host.proxy.internal
        fi
        ready="$private_dir/proxy-forward.ready"
        : > "$ready"
        set -- "$private_dir/host-proxy.json" --bind "$bind" --container-host "$host" --ready-file "$ready"
        if [ "$proxy_docker_sudo" = true ]; then set -- "$@" --docker-sudo; fi
        nohup sh "$deployment_dir/proxy-forward.sh" "$@" \
          > "$private_dir/proxy-forward.log" 2>&1 < /dev/null &
        forward_pid=$!
        echo "$forward_pid" > "$private_dir/proxy-forward.pid"
        attempt=0
        until [ -s "$ready" ]; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 15 ] || ! kill -0 "$forward_pid" 2>/dev/null; then
                echo 'Host forwarding did not start; see runtime-data/.deployment/proxy-forward.log. HOST clients remain unavailable.' >&2
                break
            fi
            sleep 1
        done
        ;;
esac
verification=$(prepare --verify)
printf '%s\n' "$verification"
case "$verification" in
    *'Proxy validation failed: yes'*)
        action=${PREDICTION_AGENT_PROXY_FAILURE_ACTION:-}
        if [ -z "$action" ] && [ -t 0 ]; then
            printf '%s\n' '检测到了系统代理，但容器无法通过它完成 HTTPS 请求。'
            printf '%s' '输入 1 不使用代理并继续，输入 2 退出：[1/2] '
            IFS= read -r action
        fi
        case "$action" in
            1|direct|DIRECT)
                prepare --force-direct
                printf '%s\n' '已按用户选择改为直连。'
                ;;
            *)
                printf '%s\n' '代理不可用，已退出且未启动机器人。可设置 PREDICTION_AGENT_PROXY_FAILURE_ACTION=direct 明确选择直连。' >&2
                exit 1
                ;;
        esac
        ;;
esac
