#!/usr/bin/env bash
# One command to run the local decision model. Everything it needs beyond Node is fetched here.
#
# Run this bundle separately on a machine with working browser WebGPU. The robot image only serves
# the download and checks a configured address; it does not launch this service inside the container.
set -euo pipefail
cd "$(dirname "$0")"

command -v node >/dev/null || { echo "需要 Node.js 18 以上：https://nodejs.org"; exit 1; }
command -v curl >/dev/null || { echo "需要 curl 来检查 GitHub 上的 webtorch SDK"; exit 1; }

# Playwright drives the browser; the browser itself is installed by the service when it finds none.
[ -d node_modules/playwright ] || npm install --no-audit --no-fund

exec node server.mjs --webtorch ./vendor/webtorch "$@"
