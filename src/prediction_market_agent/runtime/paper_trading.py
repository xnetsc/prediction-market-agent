"""Running the whole robot for real, except for the part that spends money.

An operator cannot judge this thing from its configuration. What matters is whether the markets it
picks, the reasoning it produces and the trades it proposes are any good, and the only honest way to
see that is to let it run - which normally means funding a wallet and letting it trade before anyone
knows whether it is worth funding.

So everything runs: the real platform, the real prices, the real model, the real ledger, the real
settlement. Only the order stops at the door. It is filled here instead, at the price the venue
actually quoted, and the position, the cash and the realised result are booked exactly as a real one
would be - which is what makes the outcome readable later. A declared balance stands in for money,
because a robot told it has nothing will correctly refuse to do anything, and a run where nothing
happens teaches nobody anything.

Two things this is careful about. The fill is at the quoted price with the quoted fee, and no better:
a simulator that fills at the price you wanted is a machine for producing encouraging numbers. And
the account's figures are marked as simulated wherever they surface, because the one failure that
would make this worse than useless is an operator mistaking these results for money they made.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

LOGGER = logging.getLogger(__name__)

SIMULATED = "simulated"
"""What `AccountFunds.source` says here, so nothing downstream mistakes this for a platform figure."""


class PaperWriteTransport:
    """Real quotes from the venue, fills that never leave this machine.

    Wraps the plugin's own transport rather than replacing it, so everything that only reads - the
    quote, and its price, fee and expiry - is exactly what a live run would have got. Only the four
    calls that change something at the venue are answered locally.
    """

    def __init__(self, real: Any):
        self._real = real
        self.orders: dict[str, dict[str, Any]] = {}

    def __getattr__(self, item: str) -> Any:
        # Anything beyond the write contract belongs to the plugin and is not ours to imitate.
        return getattr(self._real, item)

    def get_quote(self, **values: Any) -> dict[str, Any]:
        """The venue's real price. Guessing it would make every number downstream fiction."""
        return self._real.get_quote(**values)

    def place_order(self, **values: Any) -> dict[str, Any]:
        """Fill at the quote, immediately and completely - and never at a better price.

        A paper fill is the most optimistic thing in any simulation: no queue, no partial, no
        slippage. Being explicit about that is the point. What it must not also do is improve on the
        price the venue quoted, which would turn the exercise into a machine for encouraging numbers.
        """
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        order = {
            "orderId": order_id,
            "status": "FILLED",
            "quoteId": values.get("quote_id", ""),
            "simulated": True,
            "placed_at": int(time.time() * 1000),
        }
        self.orders[order_id] = order
        LOGGER.info("paper fill %s for quote %s", order_id, values.get("quote_id", ""))
        return order

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        return {
            "simulated": True,
            "canceled": [str(item) for item in order_ids if str(item) in self.orders],
            "failed": [
                {"orderId": str(item), "reason": "unknown paper order"}
                for item in order_ids
                if str(item) not in self.orders
            ],
        }

    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]:
        """Settlement is booked locally, but on the platform's real answer about who won."""
        return {"simulated": True, "redeemed": [str(item) for item in outcome_ids]}

    def transfer(self, direction: str, amount: str) -> dict[str, Any]:
        return {"simulated": True, "direction": direction, "amount": str(amount)}


class PaperMarketApi:
    """A platform that behaves exactly as itself, other than spending money.

    Reads, capabilities, settlement answers and funding requests all go through untouched; the
    balance is the operator's declared figure and says so, and writes are built on the paper
    transport. Delegating everything else by attribute means a plugin offering more than the
    contract keeps working here too.
    """

    def __init__(self, plugin: Any, declared_funds: float):
        self._plugin = plugin
        self._declared = float(declared_funds)

    @property
    def name(self) -> str:
        return self._plugin.name

    @property
    def capabilities(self) -> Any:
        return self._plugin.capabilities

    def __getattr__(self, item: str) -> Any:
        return getattr(self._plugin, item)

    def account_funds(self) -> Any:
        """The declared figure, marked as declared, with the real one alongside it.

        A robot told it has nothing correctly refuses to do anything, so a paper run needs a number
        to size against. Reporting the platform's real balance next to it keeps the difference in
        front of whoever reads this: one of these is money and the other is not.
        """
        from ..plugin_system.contracts import AccountFunds

        detail: dict[str, Any] = {
            "why": "纸面交易模式：这是你填的模拟金额，不是真钱，也不是平台余额",
            "paper_trading": True,
        }
        try:
            real = self._plugin.account_funds()
            detail["platform_actually_has"] = real.available
            currency = real.currency
        except Exception as error:
            detail["platform_balance_error"] = str(error)[:200]
            currency = ""
        return AccountFunds(
            available=self._declared, currency=currency, source=SIMULATED, detail=detail
        )

    def ensure_funds(self, amount: float, currency: str, **options: Any) -> Any:
        """Granted on the spot, up to the declared figure, and refused beyond it.

        The operator already answered this question by choosing a number. Asking them again per
        request would test their patience rather than the robot, and silently granting any amount
        would hide the one behaviour worth watching: what it does when it cannot have what it asked
        for.
        """
        from ..plugin_system.contracts import FundingResult

        del options
        satisfied = amount <= self._declared
        return FundingResult(
            request_id="",
            state="satisfied" if satisfied else "refused",
            requested=amount,
            currency=currency,
            available=self._declared,
            action="paper_trading",
            detail=(
                "纸面交易模式：模拟金额已满足，没有真实资金变动"
                if satisfied
                else f"纸面交易模式：模拟金额上限是 {self._declared}，本次请求超出，未满足"
            ),
        )

    def create_write_gateway(self, state: Any) -> Any:
        gateway = self._plugin.create_write_gateway(state)
        gateway.write_transport = PaperWriteTransport(gateway.write_transport)
        return gateway
