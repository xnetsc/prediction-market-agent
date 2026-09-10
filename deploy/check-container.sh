#!/bin/sh
set -eu
image_ref=${1:?Provide an image reference}
container_id=$(docker run -d -p 127.0.0.1::8765 "$image_ref")
trap 'docker rm -fv "$container_id" >/dev/null' EXIT
attempt=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id")" = healthy ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 90 ]; then
        docker logs "$container_id"
        exit 1
    fi
    sleep 2
done
docker exec "$container_id" prediction-market-agent --help >/dev/null
docker exec "$container_id" codex --version
docker exec "$container_id" claude --version
docker exec -i "$container_id" python - <<'PY'
import json
import urllib.error
import urllib.request

base = "http://127.0.0.1:8765"
assert "LOCAL_ACCESS=true" in urllib.request.urlopen(base).read().decode()
body = json.dumps({"url": "/api/settings", "body": None}).encode()
request = urllib.request.Request(base + "/api/local", data=body,
                                 headers={"Content-Type": "application/json"})
assert "fields" in json.load(urllib.request.urlopen(request))
remote = urllib.request.Request(base, headers={"Host": "public.example"})
assert "初始化管理员" in urllib.request.urlopen(remote).read().decode()
remote = urllib.request.Request(base + "/api/local", data=body,
    headers={"Host": "public.example", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(remote)
except urllib.error.HTTPError as error:
    assert error.code == 403
else:
    raise AssertionError("Public access must not use the local endpoint")
print("Installed container: CLI, local plaintext, remote authentication verified")
PY
