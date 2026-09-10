from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from prediction_market_agent.plugin_system.config_io import resolve_plugin_proxy


def _network_rules(raw: str, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    for name in ("schemes", "hosts", "methods", "paths_by_method"):
        if name not in value:
            raise ValueError(f"{field} is missing {name}")
    if not all(isinstance(value[name], list) for name in ("schemes", "hosts", "methods")):
        raise ValueError(f"{field} schemes, hosts, and methods must be arrays")
    if not isinstance(value["paths_by_method"], dict):
        raise ValueError(f"{field} paths_by_method must be an object")
    return value


@dataclass(frozen=True)
class BinancePluginConfig:
    base_url: str
    api_key: str
    api_secret: str
    wallet_address: str
    wallet_id: str
    account_type: str
    slippage_bps: int
    http_proxy: str
    network_rules: dict[str, Any]
    scan_interval_seconds: int
    error_backoff_seconds: int
    error_backoff_max_seconds: int
    max_topics_per_cycle: int
    max_decisions_per_cycle: int
    topic_page_size: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "BinancePluginConfig":
        get = values.get
        value = cls(
            base_url=get("BINANCE_API_BASE_URL", "").strip(),
            api_key=get("BINANCE_API_KEY", "").strip(),
            api_secret=get("BINANCE_API_SECRET", "").strip(),
            wallet_address=get("BINANCE_PREDICTION_WALLET_ADDRESS", "").strip(),
            wallet_id=get("BINANCE_PREDICTION_WALLET_ID", "").strip(),
            account_type=get("BINANCE_PREDICTION_ACCOUNT_TYPE", "").strip().upper(),
            slippage_bps=int(get("BINANCE_PREDICTION_SLIPPAGE_BPS", "0")),
            http_proxy=resolve_plugin_proxy(
                get("BINANCE_HTTP_PROXY", ""), field_name="BINANCE_HTTP_PROXY"
            ),
            network_rules=_network_rules(
                get("BINANCE_NETWORK_RULES_JSON", ""), "BINANCE_NETWORK_RULES_JSON"
            ),
            scan_interval_seconds=int(get("BINANCE_SCAN_INTERVAL_SECONDS", "60")),
            error_backoff_seconds=int(get("BINANCE_ERROR_BACKOFF_SECONDS", "30")),
            error_backoff_max_seconds=int(
                get("BINANCE_ERROR_BACKOFF_MAX_SECONDS", "900")
            ),
            max_topics_per_cycle=int(get("BINANCE_MAX_TOPICS_PER_CYCLE", "10")),
            max_decisions_per_cycle=int(
                get("BINANCE_MAX_DECISIONS_PER_CYCLE", "6")
            ),
            topic_page_size=int(get("BINANCE_TOPIC_PAGE_SIZE", "100")),
        )
        if not value.base_url.startswith("https://"):
            raise ValueError("BINANCE_API_BASE_URL must start with https://")
        if value.account_type not in {"SPOT", "FUNDING"}:
            raise ValueError("BINANCE_PREDICTION_ACCOUNT_TYPE must be SPOT or FUNDING")
        if not 1 <= value.slippage_bps <= 10_000:
            raise ValueError("BINANCE_PREDICTION_SLIPPAGE_BPS must be in [1, 10000]")
        if min(
            value.scan_interval_seconds,
            value.error_backoff_seconds,
            value.error_backoff_max_seconds,
            value.max_topics_per_cycle,
            value.max_decisions_per_cycle,
            value.topic_page_size,
        ) <= 0:
            raise ValueError("Binance runtime interval, backoff, and cycle limits must be positive")
        if value.error_backoff_max_seconds < value.error_backoff_seconds:
            raise ValueError(
                "BINANCE_ERROR_BACKOFF_MAX_SECONDS cannot be less than "
                "BINANCE_ERROR_BACKOFF_SECONDS"
            )
        return value

    def manifest(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_credentials_present": bool(self.api_key and self.api_secret),
            "wallet_address_present": bool(self.wallet_address),
            "wallet_id_present": bool(self.wallet_id),
            "account_type": self.account_type,
            "slippage_bps": self.slippage_bps,
            "http_proxy": self.http_proxy or "DIRECT",
            "network_rules": self.network_rules,
            "runtime": {
                "scan_interval_seconds": self.scan_interval_seconds,
                "error_backoff_seconds": self.error_backoff_seconds,
                "error_backoff_max_seconds": self.error_backoff_max_seconds,
                "max_topics_per_cycle": self.max_topics_per_cycle,
                "max_decisions_per_cycle": self.max_decisions_per_cycle,
                "topic_page_size": self.topic_page_size,
            },
        }
