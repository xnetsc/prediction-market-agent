from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any



class BinancePredictionReadClient:
    """Binance-specific signed and public read transport."""

    def __init__(self, api_key: str, api_secret: str, base_url: str, http_proxy: str = ""):
        self.api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self.base_url = base_url.rstrip("/")
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
        request = urllib.request.Request(url=url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance HTTP {error.code}: {body[:500]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Binance connection failed: {error.reason}") from error

    def spot_balances(self) -> dict[str, dict[str, float]]:
        """Free and locked balances from the signed spot account endpoint.

        This is the wallet an INBOUND transfer draws on, so it answers both "what is really there"
        and "can a top-up succeed" with one signed call the API key already has rights to make.
        """
        payload = self._request_get("/api/v3/account", {}, signed=True)
        return {
            str(item["asset"]).upper(): {
                "free": float(item.get("free", 0) or 0),
                "locked": float(item.get("locked", 0) or 0),
            }
            for item in payload.get("balances", [])
        }

    def deposit_networks(self, coin: str) -> list[dict[str, Any]]:
        """Which chains this exchange will accept one coin on, as it says itself.

        A deposit on the wrong chain is unrecoverable, and which chains an exchange takes for a
        coin changes without notice - so this is asked rather than remembered, and what it answers
        is what the operator is shown.
        """
        payload = self._request_get("/sapi/v1/capital/config/getall", {}, signed=True)
        wanted = str(coin).upper()
        for item in payload if isinstance(payload, list) else []:
            if str(item.get("coin", "")).upper() != wanted:
                continue
            return [
                {
                    "network": str(entry.get("network", "")),
                    "name": str(entry.get("name", "")),
                    "deposit_open": bool(entry.get("depositEnable", False)),
                    "minimum_confirmations": entry.get("minConfirm"),
                    "confirmations_before_withdrawal": entry.get("unLockConfirm"),
                    "contract": str(entry.get("contractAddress", "")),
                    "needs_memo": bool(str(entry.get("memoRegex", "")).strip()),
                    "note": str(entry.get("specialTips", "") or entry.get("depositDesc", "")),
                    "default": bool(entry.get("isDefault", False)),
                }
                for entry in (item.get("networkList") or [])
                if isinstance(entry, dict)
            ]
        return []

    def deposit_address(self, coin: str, network: str = "") -> dict[str, Any]:
        """The address this account deposits that coin to, on that chain."""
        params: dict[str, Any] = {"coin": str(coin).upper()}
        if network:
            params["network"] = str(network).upper()
        payload = self._request_get("/sapi/v1/capital/deposit/address", params, signed=True)
        answer = payload if isinstance(payload, dict) else {}
        return {
            "address": str(answer.get("address", "")),
            "memo": str(answer.get("tag", "")),
            "coin": str(answer.get("coin", coin)).upper(),
            "network": str(answer.get("network", network) or network).upper(),
            "url": str(answer.get("url", "")),
        }

    def deposit_history(self, coin: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Recent deposits as the exchange recorded them - the only authority on crediting."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if coin:
            params["coin"] = str(coin).upper()
        payload = self._request_get("/sapi/v1/capital/deposit/hisrec", params, signed=True)
        return [item for item in (payload if isinstance(payload, list) else []) if isinstance(item, dict)]

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
