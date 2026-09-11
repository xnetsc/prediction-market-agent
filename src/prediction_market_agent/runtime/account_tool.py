"""The account readout the model can ask for, so nothing has to decide on its behalf.

There is no stop loss, take profit or allocation rule anywhere in this program. That is deliberate:
where to stop is a judgement, and a judgement belongs either to whoever wrote the decision strategy
(stated in its own text, applied by the model) or to a business-risk plugin the operator enabled. A
judgement hard-coded in the framework would be one nobody chose and nobody can see.

Either way the judgement needs numbers, so this hands them over on request rather than padding every
decision payload with figures most rounds never look at.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

TOOL_NAME = "READ_ACCOUNT"

DESCRIPTION: dict[str, Any] = {
    "description": (
        "Read this platform's account book: starting capital, cash, exposure, equity, realised "
        "profit and loss, money transferred out, and the resulting net result. Use it to judge "
        "whether to keep a position, cut it, or take profit. No limit is enforced for you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_positions": {
                "type": "boolean",
                "description": "Include every open position and order, not just the totals.",
            }
        },
        "required": [],
        "additionalProperties": False,
    },
}


def account_snapshot(state: Any, *, include_positions: bool = False) -> dict[str, Any]:
    """Totals first, detail only when asked, so a routine check stays cheap in context."""
    net_result = state.equity + state.transferred_out - state.starting_capital
    snapshot: dict[str, Any] = {
        "starting_capital": round(state.starting_capital, 6),
        "cash": round(state.cash, 6),
        "exposure": round(state.exposure, 6),
        "equity": round(state.equity, 6),
        "realized_pnl": round(state.realized_pnl, 6),
        "transferred_out": round(state.transferred_out, 6),
        "net_result": round(net_result, 6),
        "net_result_meaning": (
            "equity + transferred_out - starting_capital; negative is a loss against the money "
            "this account started with, and a withdrawal does not count as a loss"
        ),
        "open_positions": len(state.positions),
        "open_orders": sum(1 for order in state.orders if order.status == "OPEN"),
    }
    if include_positions:
        snapshot["positions"] = [asdict(item) for item in state.positions.values()]
        snapshot["orders"] = [
            asdict(order) for order in state.orders if order.status == "OPEN"
        ]
    return snapshot


class AccountReadContribution:
    """Always available: a model that cannot see its own book cannot apply any rule about it."""

    descriptions = {TOOL_NAME: DESCRIPTION}

    def __init__(self, state: Any):
        self._state = state

    def create(self, context: Any) -> "AccountReadContribution":
        del context
        return self

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name.strip().upper() != TOOL_NAME:
            raise KeyError(name)
        return account_snapshot(
            self._state, include_positions=bool(arguments.get("include_positions"))
        )
