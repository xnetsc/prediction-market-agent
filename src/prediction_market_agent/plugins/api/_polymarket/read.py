from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import PolymarketPluginConfig


class PolymarketReadClient:
    """Public Gamma and CLOB V2 reader."""

    def __init__(self, settings: PolymarketPluginConfig):
        self.gamma_url = settings.gamma_url.rstrip("/")
        self.clob_url = settings.clob_url.rstrip("/")
        handler = (
            urllib.request.ProxyHandler({"http": settings.http_proxy, "https": settings.http_proxy})
            if settings.http_proxy
            else urllib.request.ProxyHandler({})
        )
        self.opener = urllib.request.build_opener(handler)

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{base}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "prediction-market-agent/0.5"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Polymarket read HTTP {error.code}: {detail[:500]}") from error

    def sync_time(self) -> int:
        value = self._get(self.clob_url, "/time")
        if isinstance(value, dict):
            value = value.get("timestamp", value.get("time", 0))
        return int(value)

    def list_events(self, *, offset: int, limit: int) -> list[dict[str, Any]]:
        value = self._get(
            self.gamma_url,
            "/events",
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            },
        )
        if not isinstance(value, list):
            raise RuntimeError("Polymarket Gamma /events response must be an array")
        return [item for item in value if isinstance(item, dict)]

    def get_event(self, event_id: str) -> dict[str, Any]:
        value = self._get(self.gamma_url, f"/events/{urllib.parse.quote(event_id, safe='')}")
        if not isinstance(value, dict):
            raise RuntimeError("Polymarket Gamma event response must be an object")
        return value

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        value = self._get(self.clob_url, "/book", {"token_id": token_id})
        if not isinstance(value, dict):
            raise RuntimeError("Polymarket CLOB book response must be an object")
        return value

    def get_price_history(
        self, token_id: str, *, interval: str = "1d", fidelity: int = 5
    ) -> list[dict[str, Any]]:
        value = self._get(
            self.clob_url,
            "/prices-history",
            {"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        rows = value.get("history", []) if isinstance(value, dict) else []
        return [item for item in rows if isinstance(item, dict)]
