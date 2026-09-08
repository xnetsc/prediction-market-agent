from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .managed_config import atomic_write_text


def resolve_plugin_proxy(value: str, *, field_name: str = "HTTP_PROXY") -> str:
    """Resolve DIRECT/SYSTEM/explicit proxy values for use inside a plugin."""
    value = value.strip()
    if not value or value.upper() == "DIRECT":
        return ""
    if value.upper() != "SYSTEM":
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"{field_name} must be DIRECT, SYSTEM, or an http(s) URL")
        return value
    if os.uname().sysname != "Darwin":
        raise ValueError(f"{field_name}=SYSTEM is currently supported only on macOS")
    completed = subprocess.run(
        ["scutil", "--proxy"], text=True, capture_output=True, timeout=5, check=False
    )
    if completed.returncode != 0:
        raise ValueError("Unable to read the macOS system proxy")
    enabled = re.search(r"HTTPSEnable\s*:\s*1", completed.stdout)
    host = re.search(r"HTTPSProxy\s*:\s*(\S+)", completed.stdout)
    port = re.search(r"HTTPSPort\s*:\s*(\d+)", completed.stdout)
    if not enabled or not host or not port:
        raise ValueError("macOS has no enabled HTTPS proxy")
    return f"http://{host.group(1)}:{port.group(1)}"


def json_file_callbacks(
    path: Path,
) -> tuple[Callable[[], dict[str, Any]], Callable[[dict[str, Any]], None], dict[str, Any]]:
    """Optional utility for plugins that choose a local JSON file as their storage implementation."""
    resolved = path.expanduser().resolve()

    def load() -> dict[str, Any]:
        if not resolved.exists():
            return {}
        try:
            value = json.loads(resolved.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Plugin configuration is invalid JSON: {resolved}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"Plugin configuration must be a JSON object: {resolved}")
        return value

    def save(value: dict[str, Any]) -> None:
        atomic_write_text(
            resolved,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    return load, save, {"kind": "json_file", "location": str(resolved)}
