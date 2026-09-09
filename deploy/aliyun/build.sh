#!/bin/sh
set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$deploy_dir/../.." && pwd)
build_dir="$deploy_dir/build"
temporary_dir=$(mktemp -d "$deploy_dir/.build.XXXXXX")
wheel_dir=$(mktemp -d "$deploy_dir/.wheel.XXXXXX")
cleanup() {
  rm -rf "$temporary_dir"
  rm -rf "$wheel_dir"
}
trap cleanup EXIT INT TERM
python3 -m pip wheel --no-deps --wheel-dir "$wheel_dir" "$project_dir"
docker run --rm --platform linux/amd64 \
  -v "$wheel_dir:/wheels:ro" \
  -v "$temporary_dir:/output" \
  python:3.11-slim \
  /bin/sh -c 'python -m pip install /wheels/prediction_market_agent-*.whl --target /output'
if [ -d "$build_dir" ]; then
  find "$build_dir" -mindepth 1 -delete
else
  mkdir -p "$build_dir"
fi
cp -R "$temporary_dir"/. "$build_dir"/
