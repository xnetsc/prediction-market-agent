"""Backward-compatible client proxy wrapper around the shared resolver."""
from __future__ import annotations

from pathlib import Path

from prediction_market_agent.plugin_system.config_io import resolve_proxy_settings


def client_proxy(value: str, snapshot_file: Path) -> dict:
    return resolve_proxy_settings(value, snapshot_file=snapshot_file)
