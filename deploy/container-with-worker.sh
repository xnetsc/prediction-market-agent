#!/bin/sh
set -eu

# Compatibility entry: the Web process now activates plugin-owned event loops itself.
exec /app/deploy/container-entrypoint.sh
