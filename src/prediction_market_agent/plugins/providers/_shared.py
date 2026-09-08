from __future__ import annotations

import os
import shutil
from pathlib import Path

from prediction_market_agent.agent.decision import DecisionProviderError


def subprocess_environment(proxy: str) -> dict[str, str]:
    """Pass runtime necessities without leaking unrelated plugin configuration."""
    exact = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM",
        "COLORTERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name in exact or name.startswith("LC_")
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if proxy:
            env[name] = proxy
        else:
            env.pop(name, None)
    return env


def resolve_executable(configured: str) -> str:
    if "/" in configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise DecisionProviderError(f"Executable is unavailable: {configured}")
    resolved = shutil.which(configured)
    if not resolved:
        raise DecisionProviderError(f"Executable is not on PATH: {configured}")
    return resolved
