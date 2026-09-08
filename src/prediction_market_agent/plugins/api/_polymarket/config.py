from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from prediction_market_agent.sdk.config_io import resolve_plugin_proxy


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
    http_proxy: str
    network_rules: dict[str, Any]

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
            http_proxy=resolve_plugin_proxy(
                get("POLYMARKET_HTTP_PROXY", ""), field_name="POLYMARKET_HTTP_PROXY"
            ),
            network_rules=_network_rules(
                get("POLYMARKET_NETWORK_RULES_JSON", ""),
                "POLYMARKET_NETWORK_RULES_JSON",
            ),
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
            "network_rules": self.network_rules,
        }
