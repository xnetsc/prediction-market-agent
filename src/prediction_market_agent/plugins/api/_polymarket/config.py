from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from prediction_market_agent.plugin_system.config_io import resolve_plugin_proxy


@dataclass(frozen=True)
class PolymarketPluginConfig:
    gamma_url: str
    clob_url: str
    data_url: str
    relayer_url: str
    rpc_url: str
    chain_id: int
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str
    funder_address: str
    builder_code: str
    relayer_api_key: str
    relayer_api_key_address: str
    builder_api_key: str
    builder_api_secret: str
    builder_api_passphrase: str
    transfer_recipient: str
    trading_capital: float
    http_proxy: str
    scan_interval_seconds: int
    error_backoff_seconds: int
    error_backoff_max_seconds: int
    max_topics_per_cycle: int
    max_decisions_per_cycle: int
    topic_page_size: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "PolymarketPluginConfig":
        get = values.get
        value = cls(
            gamma_url=get("POLYMARKET_GAMMA_URL", "").strip(),
            clob_url=get("POLYMARKET_CLOB_URL", "").strip(),
            data_url=get("POLYMARKET_DATA_URL", "").strip(),
            relayer_url=get("POLYMARKET_RELAYER_URL", "").strip(),
            rpc_url=get("POLYMARKET_RPC_URL", "").strip(),
            chain_id=int(get("POLYMARKET_CHAIN_ID", "0")),
            private_key=get("POLYMARKET_PRIVATE_KEY", "").strip(),
            api_key=get("POLYMARKET_API_KEY", "").strip(),
            api_secret=get("POLYMARKET_API_SECRET", "").strip(),
            api_passphrase=get("POLYMARKET_API_PASSPHRASE", "").strip(),
            funder_address=get("POLYMARKET_FUNDER_ADDRESS", "").strip(),
            builder_code=get("POLYMARKET_BUILDER_CODE", "").strip(),
            relayer_api_key=get("POLYMARKET_RELAYER_API_KEY", "").strip(),
            relayer_api_key_address=get("POLYMARKET_RELAYER_API_KEY_ADDRESS", "").strip(),
            builder_api_key=get("POLYMARKET_BUILDER_API_KEY", "").strip(),
            builder_api_secret=get("POLYMARKET_BUILDER_API_SECRET", "").strip(),
            builder_api_passphrase=get("POLYMARKET_BUILDER_API_PASSPHRASE", "").strip(),
            transfer_recipient=get("POLYMARKET_TRANSFER_RECIPIENT", "").strip(),
            trading_capital=float(get("POLYMARKET_TRADING_CAPITAL", "0") or 0),
            http_proxy=resolve_plugin_proxy(
                get("POLYMARKET_HTTP_PROXY", ""), field_name="POLYMARKET_HTTP_PROXY"
            ),
            scan_interval_seconds=int(get("POLYMARKET_SCAN_INTERVAL_SECONDS", "60")),
            error_backoff_seconds=int(get("POLYMARKET_ERROR_BACKOFF_SECONDS", "30")),
            error_backoff_max_seconds=int(
                get("POLYMARKET_ERROR_BACKOFF_MAX_SECONDS", "900")
            ),
            max_topics_per_cycle=int(get("POLYMARKET_MAX_TOPICS_PER_CYCLE", "10")),
            max_decisions_per_cycle=int(
                get("POLYMARKET_MAX_DECISIONS_PER_CYCLE", "6")
            ),
            topic_page_size=int(get("POLYMARKET_TOPIC_PAGE_SIZE", "100")),
        )
        for name, url in {
            "POLYMARKET_GAMMA_URL": value.gamma_url,
            "POLYMARKET_CLOB_URL": value.clob_url,
            "POLYMARKET_DATA_URL": value.data_url,
            "POLYMARKET_RELAYER_URL": value.relayer_url,
            "POLYMARKET_RPC_URL": value.rpc_url,
        }.items():
            if not url.startswith("https://"):
                raise ValueError(f"{name} must start with https://")
        if value.chain_id <= 0:
            raise ValueError("POLYMARKET_CHAIN_ID must be positive")
        if min(
            value.scan_interval_seconds,
            value.error_backoff_seconds,
            value.error_backoff_max_seconds,
            value.max_topics_per_cycle,
            value.max_decisions_per_cycle,
            value.topic_page_size,
        ) <= 0:
            raise ValueError(
                "Polymarket runtime interval, backoff, and cycle limits must be positive"
            )
        if value.error_backoff_max_seconds < value.error_backoff_seconds:
            raise ValueError(
                "POLYMARKET_ERROR_BACKOFF_MAX_SECONDS cannot be less than "
                "POLYMARKET_ERROR_BACKOFF_SECONDS"
            )
        return value

    def manifest(self) -> dict[str, Any]:
        return {
            "gamma_url": self.gamma_url,
            "clob_url": self.clob_url,
            "data_url": self.data_url,
            "relayer_url": self.relayer_url,
            "rpc_url": self.rpc_url,
            "chain_id": self.chain_id,
            "private_key_present": bool(self.private_key),
            "l2_credentials_present": bool(
                self.api_key and self.api_secret and self.api_passphrase
            ),
            "funder_address_present": bool(self.funder_address),
            "builder_code_present": bool(self.builder_code),
            "relayer_credentials_present": bool(
                self.relayer_api_key and self.relayer_api_key_address
            ),
            "builder_relayer_credentials_present": bool(
                self.builder_api_key
                and self.builder_api_secret
                and self.builder_api_passphrase
            ),
            "transfer_recipient_present": bool(self.transfer_recipient),
            "http_proxy": self.http_proxy or "DIRECT",
            "runtime": {
                "scan_interval_seconds": self.scan_interval_seconds,
                "error_backoff_seconds": self.error_backoff_seconds,
                "error_backoff_max_seconds": self.error_backoff_max_seconds,
                "max_topics_per_cycle": self.max_topics_per_cycle,
                "max_decisions_per_cycle": self.max_decisions_per_cycle,
                "topic_page_size": self.topic_page_size,
            },
        }
