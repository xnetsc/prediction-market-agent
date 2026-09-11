"""Everything a market API can do, offered to the model as tools.

A decision needs to look before it acts: read the book, compare the same question priced on two
platforms, check what a trade would cost, look at its own account, then buy or sell. Any of those
the model cannot reach is a gap it has to guess across, so the market contract is exposed whole
rather than in the slice one round happened to need.

Nothing here filters. Reads reach the platform through the guarded plugin and writes through the
gateway, so every call passes business risk; the call itself passes agent policy on the way in.
Those two chains are the only things that may refuse, and this module asks neither for permission
nor for forgiveness - it just does what it was asked and reports what happened.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any



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


def _plain(value: Any) -> Any:
    """Reduce a plugin's return value to something JSON can carry.

    A plugin may hand back any object it likes. Passing one straight through would fail later, at
    the point the tool result is encoded for the model, with an error that names neither the tool
    nor the plugin - so anything unrecognised is flattened here instead.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _implied(bid: float, ask: float) -> dict[str, Any]:
    mid = (bid + ask) / 2 if (bid or ask) else 0.0
    return {
        "best_bid": round(bid, 6),
        "best_ask": round(ask, 6),
        "mid": round(mid, 6),
        "spread": round(ask - bid, 6),
        "implied_probability": round(mid, 6),
    }


DESCRIPTIONS: dict[str, Any] = {
    "READ_ACCOUNT": {
        "purpose": (
            "Read a platform's account book: starting capital, cash, exposure, equity, realised "
            "profit and loss, withdrawals and net result. No limit is enforced for you; decide "
            "with these numbers and whatever your strategy text tells you about stopping."
        ),
        "arguments": {"platform": "optional string", "include_positions": "optional boolean"},
    },
    "LIST_PLATFORMS": {
        "purpose": "List every connected market platform and what each one supports.",
        "arguments": {},
    },
    "LIST_TOPICS": {
        "purpose": "Page through a platform's topics. Use it to look beyond this round's shortlist.",
        "arguments": {"platform": "optional string", "offset": "optional int", "limit": "optional int"},
    },
    "GET_TOPIC": {
        "purpose": "Read one topic in full: every market and every outcome under it.",
        "arguments": {"topic_id": "required string", "platform": "optional string"},
    },
    "GET_ORDER_BOOK": {
        "purpose": "Read the order book for any market and outcome, not only the current one.",
        "arguments": {"market_id": "required string", "outcome_id": "required string", "platform": "optional string"},
    },
    "COMPARE_OUTCOMES": {
        "purpose": (
            "Price the same question side by side across platforms or outcomes. Returns each book "
            "normalised to bid, ask, mid, spread and implied probability so they can be compared."
        ),
        "arguments": {"targets": "required list of {platform, market_id, outcome_id}"},
    },
    "OUTCOME_WON": {
        "purpose": (
            "Ask the platform whether a settled outcome won, lost, or has not resolved yet. Check "
            "this before REDEEM: redeeming the wrong side pays nothing."
        ),
        "arguments": {"topic_id": "required string", "market_id": "required string", "outcome_id": "required string", "platform": "optional string"},
    },
    "SYNC_TIME": {
        "purpose": "Resynchronise the clock a platform signs requests with.",
        "arguments": {"platform": "optional string"},
    },
    "GET_QUOTE": {
        "purpose": "Ask the platform what a trade would cost, without placing it.",
        "arguments": {
            "side": "required BUY or SELL", "price": "required number", "quantity": "required number",
            "market_id": "required string", "outcome_id": "required string",
            "order_type": "optional MARKET or LIMIT", "platform": "optional string",
        },
    },
    "PLACE_ORDER": {
        "purpose": "Quote and place an order in one step. Returns the resulting order and its status.",
        "arguments": {
            "side": "required BUY or SELL", "price": "required number", "quantity": "required number",
            "market_id": "required string", "outcome_id": "required string",
            "order_type": "optional MARKET or LIMIT", "reason": "optional string",
            "platform": "optional string",
        },
    },
    "CANCEL_ORDERS": {
        "purpose": "Cancel open orders by id.",
        "arguments": {"order_ids": "required list of strings", "platform": "optional string"},
    },
    "REDEEM": {
        "purpose": "Redeem a settled outcome position.",
        "arguments": {"outcome_id": "required string", "winning": "required boolean", "platform": "optional string"},
    },
    "TRANSFER": {
        "purpose": (
            "Move money between the wallet and the trading account. INBOUND funds the account so a "
            "buy has something to spend; OUTBOUND collects what selling and redeeming earned. "
            "Check LIST_PLATFORMS for the directions a platform actually supports, and READ_ACCOUNT "
            "for what the account holds now."
        ),
        "arguments": {"direction": "required INBOUND or OUTBOUND", "amount": "required number", "platform": "optional string"},
    },
}


class MarketToolset:
    """Dispatcher over every platform, so a tool can reach the one it names."""

    descriptions = DESCRIPTIONS

    def __init__(self, platforms: dict[str, Any], current_platform: str):
        self._platforms = platforms
        self._current = current_platform

    def create(self, context: Any) -> "MarketToolset":
        del context
        return self

    def _runtime(self, arguments: dict[str, Any]) -> Any:
        name = str(arguments.get("platform") or self._current)
        runtime = self._platforms.get(name)
        if runtime is None:
            raise KeyError(
                f"Unknown platform {name}; call LIST_PLATFORMS to see what is connected"
            )
        return runtime

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_{name.strip().lower()}", None)
        if handler is None:
            raise KeyError(name)
        return handler(arguments)

    # Reading -------------------------------------------------------------------------------
    def _list_platforms(self, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return {
            "platforms": [
                {
                    "platform": name,
                    "is_current": name == self._current,
                    "capabilities": runtime.plugin.capabilities.to_dict(),
                    "transfer_directions": list(
                        getattr(runtime.plugin.capabilities, "supported_transfer_directions", ())
                    ),
                    "account_balance_source": (
                        "local ledger only; this plugin exposes no wallet balance endpoint, so "
                        "READ_ACCOUNT reflects what this bot has recorded, not the chain or exchange"
                    ),
                }
                for name, runtime in self._platforms.items()
            ]
        }

    def _list_topics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        page = runtime.plugin.list_topics(
            offset=int(arguments.get("offset", 0) or 0),
            limit=int(arguments.get("limit", 20) or 20),
        )
        return _plain({"platform": runtime.plugin.name, "page": page})

    def _get_topic(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        topic_id = str(arguments["topic_id"])
        return _plain({"platform": runtime.plugin.name, "topic": runtime.plugin.get_topic(topic_id)})

    def _get_order_book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        book = runtime.plugin.get_order_book(
            str(arguments["market_id"]), str(arguments["outcome_id"])
        )
        return _plain({"platform": runtime.plugin.name, "book": book})

    def _compare_outcomes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for target in arguments.get("targets") or []:
            runtime = self._runtime(target)
            try:
                book = runtime.plugin.get_order_book(
                    str(target["market_id"]), str(target["outcome_id"])
                )
                bid = book.bids[0].price if book.bids else 0.0
                ask = book.asks[0].price if book.asks else 0.0
                rows.append(
                    {
                        "platform": runtime.plugin.name,
                        "market_id": target.get("market_id"),
                        "outcome_id": target.get("outcome_id"),
                        **_implied(bid, ask),
                    }
                )
            except Exception as error:
                # One unreachable book must not hide the others; say which failed and keep going.
                rows.append(
                    {
                        "platform": runtime.plugin.name,
                        "market_id": target.get("market_id"),
                        "outcome_id": target.get("outcome_id"),
                        "error": str(error)[:300],
                    }
                )
        priced = [row for row in rows if "implied_probability" in row]
        spread = (
            round(
                max(row["implied_probability"] for row in priced)
                - min(row["implied_probability"] for row in priced),
                6,
            )
            if len(priced) > 1
            else None
        )
        return {"compared": rows, "implied_probability_gap": spread}

    def _outcome_won(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        detail = runtime.plugin.get_topic(str(arguments["topic_id"]))
        market_id, outcome_id = str(arguments["market_id"]), str(arguments["outcome_id"])
        market = next((item for item in detail.markets if str(item.market_id) == market_id), None)
        if market is None:
            raise KeyError(f"No market {market_id} under topic {arguments['topic_id']}")
        outcome = next(
            (item for item in market.outcomes if str(item.outcome_id) == outcome_id), None
        )
        if outcome is None:
            raise KeyError(f"No outcome {outcome_id} in market {market_id}")
        won = runtime.plugin.outcome_won(detail, market, outcome)
        return {
            "platform": runtime.plugin.name,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "won": won,
            "resolved": won is not None,
        }

    def _sync_time(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        runtime.plugin.sync_time()
        return {"platform": runtime.plugin.name, "synchronised": True}

    def _read_account(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        return {
            "platform": runtime.plugin.name,
            **account_snapshot(
                runtime.state, include_positions=bool(arguments.get("include_positions"))
            ),
        }

    # Trading -------------------------------------------------------------------------------
    def _quote_arguments(self, runtime: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "token_id": str(arguments["outcome_id"]),
            "side": str(arguments["side"]).upper(),
            "price": float(arguments["price"]),
            "quantity": float(arguments["quantity"]),
            "fee_bps": int(arguments.get("fee_bps", 0) or 0),
            "market_topic_id": str(arguments.get("topic_id", "")),
            "market_id": str(arguments["market_id"]),
            "symbol": str(arguments.get("symbol", "")),
            "direction": str(arguments.get("direction", "")),
            "order_type": str(arguments.get("order_type", "MARKET")).upper(),
        }

    def _get_quote(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        quote = runtime.gateway.get_quote(**self._quote_arguments(runtime, arguments))
        return _plain({"platform": runtime.plugin.name, "quote": quote})

    def _place_order(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        quote = runtime.gateway.get_quote(**self._quote_arguments(runtime, arguments))
        order = runtime.gateway.place_order(quote, reason=str(arguments.get("reason", "")))
        return _plain({"platform": runtime.plugin.name, "quote": quote, "order": order})

    def _cancel_orders(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        ids = [str(item) for item in (arguments.get("order_ids") or [])]
        return _plain(
            {"platform": runtime.plugin.name, "result": runtime.gateway.cancel_orders(ids)}
        )

    def _redeem(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        result = runtime.gateway.redeem(
            str(arguments["outcome_id"]), bool(arguments.get("winning", False))
        )
        return _plain({"platform": runtime.plugin.name, "result": result})

    def _transfer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        result = runtime.gateway.transfer(
            str(arguments["direction"]).upper(), float(arguments["amount"])
        )
        return _plain({"platform": runtime.plugin.name, "result": result})
