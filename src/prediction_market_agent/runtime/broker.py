from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Any, Protocol

from ..core.domain import AccountState, ExecutionOrder, ExecutionQuote, Position


LOGGER = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    """An execution workflow could not be completed."""


class WriteTransport(Protocol):
    """API-plugin transport returning standard order statuses, never platform wire statuses."""

    def get_quote(self, **values: Any) -> dict[str, Any]: ...
    def place_order(self, **values: Any) -> dict[str, Any]: ...
    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]: ...
    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]: ...
    def transfer(self, direction: str, amount: str) -> dict[str, Any]: ...


@dataclass
class ExecutionGateway:
    """Common online order workflow backed by a plugin-owned transport."""

    state: AccountState
    platform: str
    write_transport: WriteTransport
    business_risk: Callable[[str, dict[str, Any]], Any] | None = None
    account_mode: str = "live"
    currency: str = "USDT"
    pnl_event_sink: Callable[[dict[str, Any]], Any] | None = None

    def _business_check(self, operation: str, **context: Any) -> None:
        """Put a write past business risk before it reaches the platform.

        Every one of these ends in a market API call, so each is a business action in its own
        right. Checking only the order would leave a cancel, a redeem and a withdrawal outside any
        rule, and a withdrawal is the most consequential of them.
        """
        if self.business_risk is None:
            return
        try:
            self.business_risk(operation, context)
        except Exception as error:
            raise ExecutionError(str(error)) from error

    def _now(self) -> int:
        return int(time.time() * 1000)

    def mark(self, token_id: str, price: float) -> None:
        if not 0 <= price <= 1:
            raise ExecutionError("Prediction mark price must be in [0, 1]")
        position = self.state.positions.get(token_id)
        if position:
            position.mark_price = price
            position.marked_at = self._now()

    def _record_pnl_event(self, **event: Any) -> None:
        """Persist an accepted financial effect without endangering the accepted action.

        Once a venue accepted a write it cannot be rolled back because the local audit database
        was unavailable.  The failure is therefore loud in logs, while the platform result still
        returns to the caller and the account mirror remains correct.
        """
        if self.pnl_event_sink is None:
            return
        payload = {
            "created_at": self._now(),
            "platform": self.platform,
            "account_mode": self.account_mode,
            "currency": self.currency,
            "cash_after": self.state.cash,
            "equity_after": self.state.equity,
            "realized_pnl_after": self.state.realized_pnl,
            **event,
        }
        try:
            self.pnl_event_sink(payload)
        except Exception:
            LOGGER.exception(
                "accepted %s action could not be written to the P&L ledger",
                event.get("event_type", "financial"),
            )

    def get_quote(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        quantity: float,
        fee_bps: int,
        market_topic_id: str | int,
        market_id: str | int,
        symbol: str,
        direction: str,
        order_type: str = "MARKET",
    ) -> ExecutionQuote:
        if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT"}:
            raise ExecutionError("Unsupported side or order type")
        if quantity <= 0 or not 0 < price < 1:
            raise ExecutionError("Quantity and price must be positive; price must be below 1")
        now = self._now()
        amount = quantity * price if side == "BUY" else quantity
        response = self.write_transport.get_quote(
            outcome_id=token_id,
            side=side,
            amount=str(amount),
            order_type=order_type,
            price_limit=str(price) if order_type == "LIMIT" else None,
            reference_price=price,
            fee_bps=fee_bps,
        )
        if not response.get("quoteId"):
            raise ExecutionError("Platform quote response has no quoteId")
        price = float(response.get("averagePrice") or response.get("chance") or price)
        if side == "BUY" and response.get("amountOut") is not None:
            quantity = int(str(response["amountOut"])) / 10**18
        quote = ExecutionQuote(
            quote_id=str(response["quoteId"]),
            token_id=token_id,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            notional=quantity * price,
            fee_bps=fee_bps,
            expires_at=int(response.get("expireAt", now + 30_000)),
            market_topic_id=market_topic_id,
            market_id=market_id,
            symbol=symbol,
            direction=direction,
        )
        return quote

    def place_order(
        self, quote: ExecutionQuote, reason: str = "", *, decision_id: int | None = None
    ) -> ExecutionOrder:
        self._business_check(
            "place_order",
            side=quote.side,
            order_type=quote.order_type,
            token_id=quote.token_id,
            market_id=quote.market_id,
            notional=quote.notional,
            price=quote.price,
            fee_bps=quote.fee_bps,
        )
        if quote.expires_at < self._now():
            raise ExecutionError("Platform quote expired")
        fee = quote.notional * quote.fee_bps / 10_000.0
        response = self.write_transport.place_order(
            quote_id=quote.quote_id,
            order_type=quote.order_type,
            price_limit=str(quote.price) if quote.order_type == "LIMIT" else None,
        )
        if not response.get("orderId"):
            raise ExecutionError("Platform order response has no orderId")
        status = str(response.get("status") or "").upper()
        if status not in {"OPEN", "FILLED", "CANCELED", "REJECTED", "FAILED"}:
            raise ExecutionError(
                "API plugin returned an unsupported normalized order status"
            )
        order = ExecutionOrder(
            order_id=str(response["orderId"]),
            quote_id=quote.quote_id,
            token_id=quote.token_id,
            side=quote.side,
            order_type=quote.order_type,
            status=status,
            quantity=quote.quantity,
            price=quote.price,
            notional=quote.notional,
            fee=fee,
            fee_bps=quote.fee_bps,
            market_topic_id=quote.market_topic_id,
            market_id=quote.market_id,
            symbol=quote.symbol,
            direction=quote.direction,
            created_at=self._now(),
            reason=reason,
        )
        if order.status == "FILLED":
            self._fill(order, quote, decision_id=decision_id)
        self.state.orders.append(order)
        return order

    def _fill(
        self, order: ExecutionOrder, quote: ExecutionQuote, *, decision_id: int | None = None
    ) -> None:
        existing = self.state.positions.get(quote.token_id)
        if quote.side == "BUY":
            total_debit = order.notional + order.fee
            self.state.cash -= total_debit
            if existing:
                combined_cost = existing.cost_basis + order.notional
                existing.quantity += order.quantity
                existing.average_price = combined_cost / existing.quantity
                existing.mark_price = order.price
                existing.marked_at = self._now()
            else:
                self.state.positions[quote.token_id] = Position(
                    token_id=quote.token_id,
                    market_topic_id=quote.market_topic_id,
                    market_id=quote.market_id,
                    symbol=quote.symbol,
                    direction=quote.direction,
                    quantity=order.quantity,
                    average_price=order.price,
                    mark_price=order.price,
                    opened_at=self._now(),
                    marked_at=self._now(),
                )
            self.state.realized_pnl -= order.fee
            self._record_pnl_event(
                event_type="BUY_FILL",
                market_topic_id=quote.market_topic_id,
                market_id=quote.market_id,
                token_id=quote.token_id,
                order_id=order.order_id,
                decision_id=decision_id,
                quantity=order.quantity,
                price=order.price,
                cash_delta=-total_debit,
                position_quantity_delta=order.quantity,
                cost_basis_delta=order.notional,
                realized_pnl_delta=-order.fee,
                fee=order.fee,
                external_flow_delta=0.0,
                evidence_status="complete",
                metadata={
                    "symbol": order.symbol,
                    "direction": order.direction,
                    "order_type": order.order_type,
                    "reason": order.reason,
                },
            )
        else:
            proceeds = order.notional - order.fee
            self.state.cash += proceeds
            # Whether a sale was allowed without an open position is the platform's call, and this
            # only runs once the platform accepted it. With nothing recorded to reduce there is no
            # basis to realise, so book the cash but leave realised P&L unknown and the position
            # map untouched.  Sale proceeds are not profit when their acquisition cost is absent.
            if existing is not None and order.quantity <= existing.quantity + 1e-9:
                basis = existing.average_price * order.quantity
                realized = proceeds - basis
                self.state.realized_pnl += realized
                existing.quantity -= order.quantity
                existing.mark_price = order.price
                existing.marked_at = self._now()
                if existing.quantity <= 1e-9:
                    del self.state.positions[quote.token_id]
            else:
                basis = None
                realized = None
                if existing is not None:
                    del self.state.positions[quote.token_id]
            self._record_pnl_event(
                event_type="SELL_FILL",
                market_topic_id=quote.market_topic_id,
                market_id=quote.market_id,
                token_id=quote.token_id,
                order_id=order.order_id,
                decision_id=decision_id,
                quantity=order.quantity,
                price=order.price,
                cash_delta=proceeds,
                position_quantity_delta=-order.quantity,
                cost_basis_delta=None if basis is None else -basis,
                realized_pnl_delta=realized,
                fee=order.fee,
                external_flow_delta=0.0,
                evidence_status="complete" if basis is not None else "partial",
                metadata={
                    "symbol": order.symbol,
                    "direction": order.direction,
                    "order_type": order.order_type,
                    "reason": order.reason,
                    **(
                        {
                            "unknown": (
                                "sell quantity exceeds recorded position quantity"
                                if existing is not None
                                else "no recorded position cost basis"
                            )
                        }
                        if basis is None
                        else {}
                    ),
                },
            )

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        self._business_check("cancel_orders", order_ids=list(order_ids))
        result = self.write_transport.cancel_orders(order_ids)
        canceled = {str(value) for value in result.get("canceled", [])}
        for order in self.state.orders:
            if order.order_id in canceled and order.status == "OPEN":
                order.status = "CANCELED"
        return result

    def redeem(
        self, token_id: str, winning: bool, *, decision_id: int | None = None
    ) -> dict[str, Any]:
        self._business_check("redeem", token_id=token_id, winning=winning)
        position = self.state.positions.get(token_id)
        result = self.write_transport.redeem([token_id])
        if position is None:
            self._record_pnl_event(
                event_type="REDEEM_UNMATCHED",
                token_id=token_id,
                decision_id=decision_id,
                evidence_status="partial",
                metadata={
                    "winning": winning,
                    "unknown": "redemption accepted without a recorded position or cost basis",
                    "platform_result": result,
                },
            )
            return result
        payout = position.quantity if winning else 0.0
        basis = position.cost_basis
        self.state.cash += payout
        realized = payout - basis
        self.state.realized_pnl += realized
        del self.state.positions[token_id]
        self._record_pnl_event(
            event_type="REDEEM",
            market_topic_id=position.market_topic_id,
            market_id=position.market_id,
            token_id=token_id,
            decision_id=decision_id,
            quantity=position.quantity,
            price=1.0 if winning else 0.0,
            cash_delta=payout,
            position_quantity_delta=-position.quantity,
            cost_basis_delta=-basis,
            realized_pnl_delta=realized,
            fee=0.0,
            external_flow_delta=0.0,
            evidence_status="complete",
            metadata={
                "winning": winning,
                "symbol": position.symbol,
                "direction": position.direction,
                "platform_result": result,
            },
        )
        return result

    def transfer(self, direction: str, amount: float) -> dict[str, Any]:
        self._business_check("transfer", direction=direction, amount=amount)
        if amount <= 0:
            raise ExecutionError("Transfer amount must be positive")
        if direction not in {"INBOUND", "OUTBOUND"}:
            raise ExecutionError("Direction must be INBOUND or OUTBOUND")
        result = self.write_transport.transfer(direction, str(amount))
        if direction == "INBOUND":
            self.state.cash += amount
        elif direction == "OUTBOUND":
            self.state.cash -= amount
            self.state.transferred_out += amount
        signed = amount if direction == "INBOUND" else -amount
        self._record_pnl_event(
            event_type="TRANSFER_IN" if direction == "INBOUND" else "TRANSFER_OUT",
            cash_delta=signed,
            realized_pnl_delta=0.0,
            fee=0.0,
            external_flow_delta=signed,
            evidence_status="complete",
            metadata={"direction": direction, "platform_result": result},
        )
        return result
