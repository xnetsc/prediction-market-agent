#!/usr/bin/env bash
# One command to run the local decision model. Everything it needs beyond Node is fetched here.
#
# It has to run where there is a GPU. A container on macOS does not have one - Docker Desktop does
# not pass the Metal device through - so this runs on the host, and the robot in the container
# reaches it at http://host.docker.internal:8899/v1.
set -euo pipefail
cd "$(dirname "$0")"

command -v node >/dev/null || { echo "需要 Node.js 18 以上：https://nodejs.org"; exit 1; }

# Playwright drives the browser; the browser itself is installed by the service when it finds none.
[ -d node_modules/playwright ] || npm install --no-audit --no-fund

exec node server.mjs --webtorch ./vendor/webtorch "$@"
