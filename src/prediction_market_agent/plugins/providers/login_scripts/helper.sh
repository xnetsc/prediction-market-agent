#!/usr/bin/env bash
# This complete script is delivered by your robot for one login attempt.
prediction_login_main() (
    set -euo pipefail
    umask 077
    helper_python=''
    fallback_python=''
    for helper_candidate in python3 python; do
        if command -v "$helper_candidate" >/dev/null 2>&1; then
            helper_candidate=$(command -v "$helper_candidate")
            if "$helper_candidate" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
                if [ -z "$fallback_python" ]; then fallback_python="$helper_candidate"; fi
                if "$helper_candidate" -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' 2>/dev/null; then
                    helper_python="$helper_candidate"
                    break
                fi
            fi
        fi
    done
    if [ -z "$helper_python" ]; then helper_python="$fallback_python"; fi
    if [ -z "$helper_python" ]; then
        echo 'Python 3.9+ is required; update Python and restart the login wizard.' >&2
        exit 1
    fi
    helper_temp=''
    cleanup() {
        if [ -n "$helper_temp" ]; then
            # mktemp creates this task-owned directory; never remove a user-supplied path.
            rm -rf -- "$helper_temp"
        fi
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if ! "$helper_python" -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' 2>/dev/null; then
        echo 'Preparing an isolated temporary Python environment for cryptography (PyPI). System Python is unchanged.'
        helper_temp=$(mktemp -d)
        "$helper_python" -m venv "$helper_temp/venv" || {
            echo 'Python venv/pip is required. Install your Python venv support and retry from the wizard.' >&2
            exit 1
        }
        helper_python="$helper_temp/venv/bin/python"
        "$helper_python" -m pip --disable-pip-version-check install --quiet 'cryptography>=49,<51'
    fi
    "$helper_python" - <<'PREDICTION_LOGIN_PY'
import base64
import json
import sys
namespace = {"__name__": "prediction_login_script"}
exec(compile(base64.b64decode("@@PYTHON@@"), "<login-helper>", "exec"), namespace)
try:
    namespace["run"](json.loads(base64.b64decode("@@CONFIG@@")))
except KeyboardInterrupt:
    print("Login helper stopped. Cancel this flow in the web wizard.")
    sys.exit(130)
except Exception:
    print("Login failed or expired. Check the network/port and restart in the web wizard.", file=sys.stderr)
    sys.exit(1)
PREDICTION_LOGIN_PY
)
prediction_login_main
