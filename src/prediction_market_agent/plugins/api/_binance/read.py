from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from prediction_market_agent.core.risk import NetworkWriteGate


class BinancePredictionReadClient:
    """Binance-specific signed and public read transport."""

    def __init__(self, api_key: str, api_secret: str, base_url: str, gate: NetworkWriteGate, http_proxy: str = ""):
        self.api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.gate = gate
        self._time_offset_ms = 0
        handler = urllib.request.ProxyHandler({"http": http_proxy, "https": http_proxy}) if http_proxy else urllib.request.ProxyHandler({})
        self._opener = urllib.request.build_opener(handler)

    def _request_get(self, path: str, params: dict[str, Any], signed: bool) -> Any:
        query_params = [(key, str(value)) for key, value in params.items() if value is not None]
        headers = {"User-Agent": "prediction-market-agent-binance/1"}
        if signed:
            query_params.append(("timestamp", str(int(time.time() * 1000) + self._time_offset_ms)))
            query = urllib.parse.urlencode(query_params)
            signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urllib.parse.urlencode(query_params)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        self.gate.check("GET", url)
        request = urllib.request.Request(url=url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance HTTP {error.code}: {body[:500]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Binance connection failed: {error.reason}") from error

    def sync_time(self) -> None:
        result = self._request_get("/api/v3/time", {}, signed=False)
        self._time_offset_ms = int(result["serverTime"]) - int(time.time() * 1000)

    def list_markets(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        return self._request_get("/sapi/v1/w3w/wallet/prediction/market/list", {"sortBy": "VOLUME", "orderBy": "DESC", "offset": offset, "limit": limit}, signed=True)

    def market_detail(self, market_topic_id: int) -> dict[str, Any]:
        return self._request_get("/sapi/v1/w3w/wallet/prediction/market/detail", {"marketTopicId": market_topic_id}, signed=True)

    def order_book(self, market_id: int, token_id: str) -> dict[str, Any]:
        return self._request_get("/sapi/v1/w3w/wallet/prediction/order-book", {"vendor": "predict_fun", "marketId": market_id, "tokenId": token_id}, signed=True)

    def klines(self, symbol: str, interval: str = "1m", limit: int = 120) -> list[list[Any]]:
        result = self._request_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}, signed=False)
        if not isinstance(result, list):
            raise RuntimeError("Unexpected kline response")
        return result
