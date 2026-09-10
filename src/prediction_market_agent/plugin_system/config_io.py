from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies

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
    if platform.system() != "Darwin":
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


def validate_proxy_selector(
    value: str, *, field_name: str = "HTTP_PROXY", allow_inherit: bool = False
) -> str:
    """Validate a proxy selector without reading the network or operating system."""
    normalized = value.strip()
    modes = {"DIRECT", "HOST", "ENVIRONMENT", "SYSTEM"}
    if allow_inherit:
        modes.add("INHERIT")
    if normalized.upper() in modes:
        return normalized.upper()
    if not normalized.startswith(("http://", "https://")):
        allowed = ", ".join(sorted(modes))
        raise ValueError(f"{field_name} must be {allowed}, or an http(s) URL")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} has an invalid proxy port") from None
    if (
        not parsed.hostname
        or (not port and parsed.netloc.endswith(":"))
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError(f"{field_name} has an invalid proxy URL")
    return normalized


def resolve_proxy_settings(
    value: str,
    *,
    field_name: str = "HTTP_PROXY",
    inherited_value: str = "DIRECT",
    inherited_no_proxy: str = "",
    snapshot_file: Path | None = None,
) -> dict[str, str]:
    """Resolve a plugin selector plus the application's shared proxy settings."""
    selected = validate_proxy_selector(value, field_name=field_name, allow_inherit=True)
    inherited = selected == "INHERIT"
    if inherited:
        selected = validate_proxy_selector(
            inherited_value, field_name="shared_http_proxy", allow_inherit=False
        )

    source, captured, bypass = ("统一代理", "", inherited_no_proxy) if inherited else ("插件独立设置", "", "")
    mode = selected.upper()
    if mode in {"HOST", "ENVIRONMENT"}:
        if mode == "HOST" and snapshot_file is not None and snapshot_file.exists():
            try:
                data = json.loads(snapshot_file.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError):
                raise ValueError("无法读取宿主机代理检测文件，请重新运行一键启动器") from None
            if data.get("status") not in {"direct", "proxy"}:
                raise ValueError("宿主机代理未就绪；请查看启动器日志，或改用直连/手动代理")
            selected = data.get("proxy", "") if data["status"] == "proxy" else "DIRECT"
            source = ("统一代理 · " if inherited else "") + str(data.get("source", "宿主机检测"))
            captured = str(data.get("captured_at", ""))
            bypass = ",".join(filter(None, (bypass, str(data.get("no_proxy", "")))))
        else:
            proxies = getproxies()
            selected = proxies.get("https") or proxies.get("http") or proxies.get("all") or "DIRECT"
            bypass = ",".join(filter(None, (bypass, str(proxies.get("no", "")))))
            detail = "当前运行环境 / 系统" + ("（未找到宿主机检测文件）" if mode == "HOST" else "")
            source = ("统一代理 · " if inherited else "") + detail

    proxy = resolve_plugin_proxy(selected, field_name=field_name)
    if proxy:
        parsed = urlsplit(proxy)
        port = parsed.port
        host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
        display = urlunsplit(
            (parsed.scheme, host + (":" + str(port) if port else ""), "", "", "")
        )
    else:
        display = "DIRECT"
    bypass_items = ["localhost", "127.0.0.1", "::1"] + [
        item.strip()
        for item in bypass.split(",")
        if item.strip() and item.strip() != "<local>"
    ]
    return {
        "proxy": proxy,
        "no_proxy": ",".join(dict.fromkeys(bypass_items)),
        "display": display,
        "source": source,
        "captured_at": captured,
        "inherited": "true" if inherited else "false",
    }


def json_file_callbacks(
    path: Path,
) -> tuple[
    Callable[[], dict[str, Any]],
    Callable[[dict[str, Any]], None],
    Callable[[], None],
    dict[str, Any],
]:
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

    def delete() -> None:
        if resolved.exists():
            resolved.unlink()

    return load, save, delete, {"kind": "json_file", "location": str(resolved)}
