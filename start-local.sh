#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --index-url https://pypi.org/simple .
if [ ! -f config/application.json ]; then
  .venv/bin/prediction-market-agent init
fi
exec .venv/bin/prediction-market-agent serve
