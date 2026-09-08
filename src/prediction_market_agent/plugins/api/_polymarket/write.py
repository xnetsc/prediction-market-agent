from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

import httpx
from polymarket import BuilderApiKey, PRODUCTION, RelayerApiKey, SecureClient
from polymarket.models import ApiKeyCreds

from prediction_market_agent.core.risk import NetworkWriteGate
from .config import PolymarketPluginConfig


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


class _QuoteSpec:
    def __init__(
        self,
        token_id: str,
        side: str,
        price: float,
        quantity: float,
        order_type: str,
        expires_at: int,
    ) -> None:
        self.token_id = token_id
        self.side = side
        self.price = price
        self.quantity = quantity
        self.order_type = order_type
        self.expires_at = expires_at


class PolymarketWriteTransport:
    """Official unified SDK adapter for orders, cancellation, redemption, and pUSD transfer."""

    def __init__(self, settings: PolymarketPluginConfig, gate: NetworkWriteGate):
        self.settings = settings
        self.gate = gate
        self._quotes: dict[str, _QuoteSpec] = {}
        self._client: SecureClient | None = None

    def _api_key(self) -> BuilderApiKey | RelayerApiKey | None:
        if (
            self.settings.builder_api_key
            and self.settings.builder_api_secret
            and self.settings.builder_api_passphrase
        ):
            return BuilderApiKey(
                key=self.settings.builder_api_key,
                secret=self.settings.builder_api_secret,
                passphrase=self.settings.builder_api_passphrase,
            )
        if self.settings.relayer_api_key and self.settings.relayer_api_key_address:
            return RelayerApiKey(
                key=self.settings.relayer_api_key,
                address=self.settings.relayer_api_key_address,
            )
        return None

    def _gated_http_client(self, base_url: str) -> httpx.Client:
        def check(request: httpx.Request) -> None:
            self.gate.check(request.method, str(request.url))

        return httpx.Client(
            base_url=base_url,
            proxy=self.settings.http_proxy or None,
            trust_env=False,
            http2=True,
            timeout=20,
            event_hooks={"request": [check]},
            headers={"User-Agent": "prediction-market-agent/0.6"},
        )

    def _install_network_policy(self, client: SecureClient) -> None:
        ctx = client._ctx  # polymarket-client 0.3.x: pinned internal transport integration point
        transports = (
            ctx.gamma,
            ctx.data,
            ctx.rfq,
            ctx.clob,
            ctx.secure_clob,
            ctx.relayer,
            ctx.combos,
            ctx.rpc._transport,
        )
        for transport in transports:
            old = transport._client
            base_url = str(old.base_url)
            old.close()
            transport._client = self._gated_http_client(base_url)
            transport._owns_client = True

    def _require_client(self) -> SecureClient:
        if self._client is not None:
            return self._client
        environment = replace(
            PRODUCTION,
            name="configured",
            chain_id=self.settings.chain_id,
            gamma_url=self.settings.gamma_url,
            clob_url=self.settings.clob_url,
            data_url=self.settings.data_url,
            relayer_url=self.settings.relayer_url,
            rpc_url=self.settings.rpc_url,
        )
        credentials = ApiKeyCreds(
            key=self.settings.api_key,
            secret=self.settings.api_secret,
            passphrase=self.settings.api_passphrase,
        )
        # Public create() performs requests during construction before an application can
        # inject transport policy. Version 0.3.x exposes this constructor path; credentials
        # are supplied, validation is deferred, and all transports are replaced before use.
        client = SecureClient._create(
            private_key=self.settings.private_key,
            wallet=self.settings.funder_address,
            environment=environment,
            credentials=credentials,
            api_key=self._api_key(),
            validate_credentials=False,
        )
        try:
            self._install_network_policy(client)
            self._client = client._ensure_wallet_ready()
        except BaseException:
            client.close()
            raise
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

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
        del fee_bps
        price = float(price_limit or reference_price)
        if not 0 < price < 1:
            raise ValueError("Polymarket quote requires an executable price in (0, 1)")
        normalized_amount = float(amount)
        quantity = normalized_amount / price if side == "BUY" else normalized_amount
        quote_id = f"poly_quote_{uuid.uuid4().hex}"
        expires_at = int(time.time() * 1000) + 30_000
        self._quotes[quote_id] = _QuoteSpec(
            outcome_id, side, price, quantity, order_type, expires_at
        )
        return {"quoteId": quote_id, "averagePrice": price, "expireAt": expires_at}

    def place_order(
        self, *, quote_id: str, order_type: str, price_limit: str | None
    ) -> dict[str, Any]:
        spec = self._quotes.pop(quote_id, None)
        if spec is None or spec.expires_at < int(time.time() * 1000):
            raise RuntimeError("Polymarket local quote is missing or expired")
        client = self._require_client()
        price = float(price_limit or spec.price)
        builder_code = self.settings.builder_code or None
        if order_type == "LIMIT":
            response = client.place_limit_order(
                token_id=spec.token_id,
                price=price,
                size=spec.quantity,
                side=spec.side,
                builder_code=builder_code,
            )
        elif spec.side == "BUY":
            response = client.place_market_order(
                token_id=spec.token_id,
                side="BUY",
                amount=spec.quantity * price,
                max_price=price,
                order_type="FOK",
                builder_code=builder_code,
            )
        else:
            response = client.place_market_order(
                token_id=spec.token_id,
                side="SELL",
                shares=spec.quantity,
                min_price=price,
                order_type="FOK",
                builder_code=builder_code,
            )
        raw = _jsonable(response)
        if not getattr(response, "ok", False):
            raise RuntimeError(
                f"Polymarket order rejected: {getattr(response, 'code', 'unknown')}: "
                f"{getattr(response, 'message', '')}"
            )
        platform_status = str(response.status)
        status = {
            "live": "OPEN",
            "delayed": "OPEN",
            "matched": "FILLED",
        }[platform_status.casefold()]
        return {
            **raw,
            "orderId": str(response.order_id),
            "platformStatus": platform_status,
            "status": status,
        }

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        response = self._require_client().cancel_orders(order_ids=order_ids)
        raw = _jsonable(response)
        return {
            **raw,
            "canceled": [str(item) for item in response.canceled],
            "failed": [
                {"orderId": str(order_id), "reason": reason}
                for order_id, reason in response.not_canceled.items()
            ],
        }

    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]:
        client = self._require_client()
        requested = set(outcome_ids)
        condition_ids: list[str] = []
        for position in client.list_positions(redeemable=True, page_size=100).iter_items():
            if str(position.token_id) in requested and str(position.condition_id) not in condition_ids:
                condition_ids.append(str(position.condition_id))
        results = []
        for condition_id in condition_ids:
            handle = client.redeem_positions(condition_id=condition_id)
            results.append(
                {"conditionId": condition_id, "transaction": _jsonable(handle.wait())}
            )
        return {
            "requestedTokenIds": list(outcome_ids),
            "redeemedConditionIds": condition_ids,
            "transactions": results,
        }

    def transfer(self, direction: str, amount: str) -> dict[str, Any]:
        if direction.upper() != "OUTBOUND":
            raise ValueError("Polymarket supports only OUTBOUND pUSD transfers")
        if not self.settings.transfer_recipient:
            raise RuntimeError("POLYMARKET_TRANSFER_RECIPIENT is required for transfer")
        amount_usdt = float(amount)
        amount_base_units = int(round(amount_usdt * 10**6))
        if amount_base_units <= 0:
            raise ValueError("Polymarket transfer amount is below one pUSD base unit")
        client = self._require_client()
        handle = client.transfer_erc20(
            token_address=client.environment.collateral_token,
            recipient_address=self.settings.transfer_recipient,
            amount=amount_base_units,
            metadata=f"Prediction agent transfer {amount_usdt:.6f} pUSD",
        )
        outcome = handle.wait()
        return {
            "status": "COMPLETED",
            "direction": "OUTBOUND",
            "amountPusd": amount_usdt,
            "amountBaseUnits": amount_base_units,
            "recipient": self.settings.transfer_recipient,
            "transaction": _jsonable(outcome),
        }
