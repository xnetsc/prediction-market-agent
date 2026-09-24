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
docker exec "$container_id" npm --version
docker exec -i "$container_id" python - <<'PY'
import json
import io
import re
import subprocess
import urllib.error
import urllib.request
import zipfile

base = "http://127.0.0.1:8765"
page = urllib.request.urlopen(base).read().decode()
assert "LOCAL_ACCESS=true" in page
script = page.split("<script>", 1)[1].split("</script>", 1)[0]
subprocess.run(["node", "--check"], input=script, text=True, check=True)
body = json.dumps({"url": "/api/settings", "body": None}).encode()
request = urllib.request.Request(base + "/api/local", data=body,
                                 headers={"Content-Type": "application/json"})
assert "fields" in json.load(urllib.request.urlopen(request))
archive = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(base + "/laya-service.zip").read()))
names = set(archive.namelist())
for expected in (
    "laya-service/start.sh",
    "laya-service/server.mjs",
    "laya-service/vendor/webtorch/UPSTREAM_SHA",
    "laya-service/vendor/webtorch/webtorch/decision.py",
):
    assert expected in names, expected
upstream = archive.read("laya-service/vendor/webtorch/UPSTREAM_SHA").decode().strip()
assert re.fullmatch(r"[0-9a-f]{40}", upstream), upstream
assert b"usage.input_tokens" in archive.read("laya-service/server.mjs")
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
print("Installed container: CLI, local plaintext, remote authentication and Laya download verified")
PY
