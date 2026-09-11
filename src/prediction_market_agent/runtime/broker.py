from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Any, Protocol

from ..core.domain import AccountState, ExecutionOrder, ExecutionQuote, Position


class ExecutionError(RuntimeError):
    """An execution workflow could not be completed."""


class WriteTransport(Protocol):
    """API-plugin transport returning standard order statuses, never platform wire statuses."""

    def get_quote(self, **values: Any) -> dict[str, Any]: ...
    def place_order(self, **values: Any) -> dict[str, Any]: ...
    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]: ...
    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]: ...
    def transfer(self, direction: str, amount: str) -> dict[str, Any]: ...


class ExecutionRiskControl(Protocol):
    """Account state a risk plugin maintains, plus the sizing question asked before quoting.

    Neither of these refuses anything. Refusal belongs to the business-risk chain, which the same
    plugin joins as a `market:<platform>` engine, so it stacks with every other enabled plugin and
    fails closed on error. What is left here is the part a chain verdict cannot express: keeping the
    halt flag current, and answering how large a buy may be before a quote is requested.
    """

    target: str

    def refresh_halt(self) -> None: ...
    def allowed_buy_notional(
        self,
        requested: float,
        current_position_value: float,
        fee_bps: int,
        token_id: str,
    ) -> float: ...


@dataclass
class ExecutionGateway:
    """Common online order workflow backed by a plugin-owned transport."""

    state: AccountState
    risk: ExecutionRiskControl
    platform: str
    write_transport: WriteTransport
    business_risk: Callable[[str, dict[str, Any]], Any] | None = None

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
        self.risk.refresh_halt()

    def allowed_buy_notional(
        self,
        requested: float,
        current_position_value: float,
        fee_bps: int,
        token_id: str,
    ) -> float:
        return self.risk.allowed_buy_notional(
            requested, current_position_value, fee_bps, token_id
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

    def place_order(self, quote: ExecutionQuote, reason: str = "") -> ExecutionOrder:
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
        existing = self.state.positions.get(quote.token_id)
        if quote.side != "BUY":
            pending_quantity = sum(
                order.quantity
                for order in self.state.orders
                if order.status == "OPEN"
                and order.side == "SELL"
                and order.token_id == quote.token_id
            )
            if not existing or quote.quantity > existing.quantity - pending_quantity + 1e-9:
                raise ExecutionError("Insufficient unreserved shares")
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
            self._fill(order, quote)
        self.state.orders.append(order)
        return order

    def _fill(self, order: ExecutionOrder, quote: ExecutionQuote) -> None:
        existing = self.state.positions.get(quote.token_id)
        if quote.side == "BUY":
            total_debit = order.notional + order.fee
            self.state.cash -= total_debit
            if existing:
                combined_cost = existing.cost_basis + order.notional
                existing.quantity += order.quantity
                existing.average_price = combined_cost / existing.quantity
                existing.mark_price = order.price
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
                )
            self.state.realized_pnl -= order.fee
        else:
            if not existing or order.quantity > existing.quantity + 1e-9:
                raise ExecutionError("Insufficient shares")
            proceeds = order.notional - order.fee
            basis = existing.average_price * order.quantity
            self.state.cash += proceeds
            self.state.realized_pnl += proceeds - basis
            existing.quantity -= order.quantity
            existing.mark_price = order.price
            if existing.quantity <= 1e-9:
                del self.state.positions[quote.token_id]
        self._refresh_halt_state()

    def _refresh_halt_state(self) -> None:
        """Recompute the halt flag after balances moved. This maintains state; it refuses nothing."""
        self.risk.refresh_halt()

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        self._business_check("cancel_orders", order_ids=list(order_ids))
        result = self.write_transport.cancel_orders(order_ids)
        canceled = {str(value) for value in result.get("canceled", [])}
        for order in self.state.orders:
            if order.order_id in canceled and order.status == "OPEN":
                order.status = "CANCELED"
        return result

    def redeem(self, token_id: str, winning: bool) -> dict[str, Any]:
        self._business_check("redeem", token_id=token_id, winning=winning)
        position = self.state.positions.get(token_id)
        if not position:
            raise ExecutionError("No position to redeem")
        result = self.write_transport.redeem([token_id])
        payout = position.quantity if winning else 0.0
        basis = position.cost_basis
        self.state.cash += payout
        self.state.realized_pnl += payout - basis
        del self.state.positions[token_id]
        self._refresh_halt_state()
        return result

    def transfer(self, direction: str, amount: float) -> dict[str, Any]:
        self._business_check("transfer", direction=direction, amount=amount)
        if amount <= 0:
            raise ExecutionError("Transfer amount must be positive")
        if direction == "OUTBOUND" and amount > self.state.cash:
            raise ExecutionError("Insufficient cash")
        if direction not in {"INBOUND", "OUTBOUND"}:
            raise ExecutionError("Direction must be INBOUND or OUTBOUND")
        result = self.write_transport.transfer(direction, str(amount))
        if direction == "INBOUND":
            self.state.cash += amount
        elif direction == "OUTBOUND":
            self.state.cash -= amount
            self.state.transferred_out += amount
        self._refresh_halt_state()
        return result
