#!/bin/sh
set -eu

workdir="${PREDICTION_AGENT_WORKDIR:-/data}"
config_file="${PREDICTION_AGENT_CONFIG_FILE:-$workdir/config/application.json}"
mkdir -p "$workdir/config/plugins"
cd "$workdir"
if [ ! -f "$config_file" ]; then
  prediction-market-agent --config "$config_file" init
fi

prediction-market-agent --config "$config_file" run &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
prediction-market-agent --config "$config_file" serve \
  --listen-host 0.0.0.0 --listen-port "${PORT:-8765}"
