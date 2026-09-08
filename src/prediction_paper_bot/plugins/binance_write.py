from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..risk import NetworkWriteGate
from .binance_config import BinancePluginConfig


class BinancePredictionWriteTransport:
    """Binance-specific signed write transport. Generic engine code never imports this class."""

    def __init__(
        self, settings: BinancePluginConfig, gate: NetworkWriteGate
    ):
        self.api_key = settings.api_key
        self.secret = settings.api_secret.encode("utf-8")
        self.base_url = settings.base_url.rstrip("/")
        self.wallet_address = settings.wallet_address
        self.wallet_id = settings.wallet_id
        self.account_type = settings.account_type
        self.slippage_bps = settings.slippage_bps
        self.gate = gate
        handler = (
            urllib.request.ProxyHandler({"http": settings.http_proxy, "https": settings.http_proxy})
            if settings.http_proxy
            else urllib.request.ProxyHandler({})
        )
        self.opener = urllib.request.build_opener(handler)

    @staticmethod
    def _form(items: list[tuple[str, Any]], preserve_brackets: bool = False) -> str:
        safe = "[]" if preserve_brackets else ""
        return urllib.parse.urlencode(
            [(key, str(value)) for key, value in items if value is not None], safe=safe
        )

    def _post(
        self, path: str, body_items: list[tuple[str, Any]], *, preserve_brackets: bool = False
    ) -> dict[str, Any]:
        self.gate.check("POST", f"{self.base_url}{path}")
        timestamp_query = f"timestamp={int(time.time() * 1000)}"
        body = self._form(body_items, preserve_brackets=preserve_brackets)
        total_params = timestamp_query + body
        signature = hmac.new(self.secret, total_params.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{timestamp_query}&signature={signature}"
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "X-MBX-APIKEY": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "prediction-market-agent/0.4",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance write HTTP {error.code}: {detail[:500]}") from error
        if not isinstance(value, dict):
            raise RuntimeError("Binance write response must be an object")
        return value

    def get_quote(
        self,
        *,
        outcome_id: str,
        side: str,
        amount: str,
        order_type: str,
        price_limit: str | None,
        reference_price: float,
        fee_bps: int,
    ) -> dict[str, Any]:
        del reference_price
        return self._post(
            "/sapi/v1/w3w/wallet/prediction/trade/get-quote",
            [
                ("walletAddress", self.wallet_address),
                ("tokenId", outcome_id),
                ("side", side),
                ("amountIn", str(int(round(float(amount) * 10**18)))),
                ("orderType", order_type),
                ("slippageBps", self.slippage_bps),
                ("priceLimit", price_limit),
                ("chainId", "56"),
                ("feeRateBps", fee_bps),
                ("fundingSource", "MPC"),
            ],
        )

    def place_order(
        self, *, quote_id: str, order_type: str, price_limit: str | None
    ) -> dict[str, Any]:
        response = self._post(
            "/sapi/v1/w3w/wallet/prediction/trade/place-order-bundle",
            [
                ("walletAddress", self.wallet_address),
                ("walletId", self.wallet_id),
                ("quoteId", quote_id),
                ("timeInForce", "FOK" if order_type == "MARKET" else "GTC"),
                ("accountType", self.account_type),
                ("orderType", order_type),
                ("slippageBps", self.slippage_bps),
                ("priceLimit", price_limit),
                ("fundingSource", "MPC"),
            ],
        )
        platform_status = str(
            response.get("orderStatus") or response.get("status") or response.get("state") or ""
        )
        normalized = platform_status.upper()
        if normalized in {"NEW", "OPEN", "PENDING", "LIVE", "PROCESSING"}:
            status = "OPEN"
        elif normalized in {"FILLED", "MATCHED", "COMPLETED", "SUCCESS"}:
            status = "FILLED"
        elif normalized in {"CANCELLED", "CANCELED"}:
            status = "CANCELED"
        elif normalized in {"REJECTED", "FAILED", "EXPIRED"}:
            status = "REJECTED"
        else:
            status = "OPEN" if order_type == "LIMIT" else "FILLED"
        return {**response, "platformStatus": platform_status, "status": status}

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        items: list[tuple[str, Any]] = [
            ("walletAddress", self.wallet_address), ("walletId", self.wallet_id)
        ]
        items.extend((f"cancelInfoList[{index}].orderId", value) for index, value in enumerate(order_ids))
        return self._post(
            "/sapi/v1/w3w/wallet/prediction/trade/batch-cancel",
            items,
            preserve_brackets=True,
        )

    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]:
        items: list[tuple[str, Any]] = [
            ("walletAddress", self.wallet_address), ("walletId", self.wallet_id)
        ]
        items.extend(("tokenIds", value) for value in outcome_ids)
        items.append(("chainId", "56"))
        return self._post("/sapi/v1/w3w/wallet/prediction/batch-redeem", items)

    def transfer(self, direction: str, amount: str) -> dict[str, Any]:
        normalized = direction.upper()
        if normalized not in {"INBOUND", "OUTBOUND"}:
            raise ValueError("Transfer direction must be INBOUND or OUTBOUND")
        items: list[tuple[str, Any]] = [
            ("walletId", self.wallet_id),
            ("walletAddress", self.wallet_address),
            ("fromTokenAmount", str(int(round(float(amount) * 10**18)))),
            ("accountType", self.account_type),
        ]
        if normalized == "OUTBOUND":
            items.append(("sourceBiz", "USER_TRANSFER"))
        return self._post(
            f"/sapi/v1/w3w/wallet/prediction/transfer/{normalized.lower()}", items
        )
