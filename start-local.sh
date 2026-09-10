#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
. "$project_dir/deploy/ensure-docker.sh"
ensure_docker
docker_run compose -f "$project_dir/compose.yaml" pull
docker_run compose -f "$project_dir/compose.yaml" up -d --no-build --wait --wait-timeout 180
echo "Robot is ready: http://127.0.0.1:${PREDICTION_AGENT_PORT:-8765}"
