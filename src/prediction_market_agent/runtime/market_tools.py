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

import time
from dataclasses import asdict, is_dataclass
from typing import Any

from ..agent.market_playbook import read_market_playbook
from ..plugin_system.contracts import FUNDING_TIMEOUT_SECONDS
from .operator_instructions import OperatorInstructions, by_urgency, instruction_detail



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
    "READ_MARKET_PLAYBOOK": {
        "purpose": (
            "Read one prediction-market guide only when needed: selection, execution, "
            "resolution, structure, feedback, or field_notes (first-person reports)."
        ),
        "arguments": {"section": "required section name"},
    },
    "READ_ACCOUNT": {
        "purpose": (
            "Read a platform's account book: starting capital, cash, exposure, equity, realised "
            "profit and loss, withdrawals and net result. No limit is enforced for you; decide "
            "with these numbers and whatever your strategy text tells you about stopping."
        ),
        "arguments": {"platform": "optional string", "include_positions": "optional boolean"},
    },
    "ACCOUNT_FUNDS": {
        "purpose": (
            "Ask the platform what the trading account can actually spend right now. The reply "
            "says where the number came from: source=platform means the platform confirmed it, "
            "source=declared means it is a configured figure that may have drifted. Not the same "
            "as READ_ACCOUNT, which is this bot's own ledger."
        ),
        "arguments": {"platform": "optional string"},
    },
    "ENSURE_FUNDS": {
        "purpose": (
            "Ask for an amount to be spendable here. The reply carries a `state`: satisfied means "
            "it is there now; pending means someone has to approve or send it, and you get a "
            "`request_id` to check later with FUNDING_STATUS; refused or failed means it will not "
            "happen. Say why you need it: a person may have to approve the transfer, and the "
            "reason is the one thing they cannot work out for themselves. Set allow_pending "
            "false if you cannot wait, and a platform that cannot finish immediately will say so "
            "instead of parking the request. If an earlier ask of yours is still waiting for "
            "someone to approve it, the platform may put a question back to you before recording "
            "this one, because only one request can be waiting at a time."
        ),
        "arguments": {
            "amount": "required number",
            "reason": "required string: why the money is needed, shown to whoever approves it",
            "currency": "optional string; defaults to whatever this platform settles in",
            "allow_pending": "optional boolean, default true",
            "platform": "optional string",
        },
    },
    "FUNDING_STATUS": {
        "purpose": (
            "Check where an earlier ENSURE_FUNDS got to, by its request_id. States are satisfied, "
            "pending, partial (some arrived - `available` says how much), refused and failed."
        ),
        "arguments": {"request_id": "required string", "platform": "optional string"},
    },
    "NOTE_INSTRUCTION": {
        "purpose": (
            "Report where you have got to on something the operator attached to their money. The "
            "instructions you were given carry an id; use it. state=progress records how far along "
            "it is and leaves it binding. state=done closes it, and is for when the thing asked "
            "for has actually happened - the money spent as required, the trade made - not when "
            "you intend to. state=expired closes one whose time has passed or that has become "
            "impossible. Closing one stops it binding later rounds, so close nothing you are not "
            "sure of: if it is only partly done, that is progress. This is for saying what "
            "happened, never for getting out of an instruction you would rather not follow, and "
            "only the operator may delete one."
        ),
        "arguments": {
            "id": "required integer, from the instruction you were given",
            "state": "required: progress, done or expired",
            "why": (
                "required string: the reason for this state, in the numbers the instruction was "
                "written in - what was asked, what has happened so far, what is left. \"They asked "
                "for 5 USDT on this market; 3 are bought, 2 to go\" is a reason. \"Making "
                "progress\" is not, and neither is \"done\": the operator has to be able to check "
                "it against what they wrote, and a round that cannot state the remainder has not "
                "measured it."
            ),
        },
    },
    "LIST_INSTRUCTIONS": {
        "purpose": (
            "List what the operator has attached to their money on this platform. You are already "
            "given the open ones with every round, so use this when you want the ones that did not "
            "fit, or the closed ones - what was asked before, and what this robot reported back "
            "about it."
        ),
        "arguments": {
            "include_closed": "optional boolean, default false",
            "platform": "optional string",
        },
    },
    "READ_INSTRUCTION": {
        "purpose": (
            "Read one instruction whole: the operator's own words, the transfer it arrived with, "
            "every condition, and where it stands. The rounds are handed the rule, not the "
            "wording, because notes are long and rules are short. Fetch the wording when it "
            "decides something - when the rule as written does not settle the case in front of "
            "you, or when you are about to close one and their sentence is what says whether it "
            "is finished."
        ),
        "arguments": {"id": "required integer"},
    },
    "LIST_PLATFORMS": {
        "purpose": "List every connected market platform and what each one supports.",
        "arguments": {},
    },
    "LIST_TOPICS": {
        "purpose": "Page through a platform's topics. Use it to look beyond this round's shortlist.",
        "arguments": {"platform": "optional string", "offset": "optional int", "limit": "optional int"},
    },
    "LIST_TOPICS_BY_DEADLINE": {
        "purpose": (
            "List a platform's topics by when they settle, soonest first, inside a window you "
            "name. The ordinary listing is ordered by how busy a market is, and the busiest ones "
            "are usually the ones settling months out - on one venue, nine of the two hundred "
            "busiest settled inside three days while asking by date returned a hundred. Use this "
            "when what you need is what resolves soon. Platforms that cannot answer by date say so."
        ),
        "arguments": {
            "within_hours": "required number: how far ahead to look",
            "platform": "optional string", "offset": "optional int", "limit": "optional int",
        },
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
        self._consultation: Any = None
        self._memory: Any = None

    def create(self, context: Any) -> "MarketToolset":
        # Taken from the standard tool context rather than wired in specially: asking the asker is
        # something any tool plugin may need, so it arrives the same way for all of them.
        self._consultation = getattr(context, "consultation", None)
        self._memory = getattr(context, "memory", None)
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

    def _read_market_playbook(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return read_market_playbook(arguments)

    # Reading -------------------------------------------------------------------------------
    def _account_funds(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        return {"platform": runtime.plugin.name, **runtime.plugin.account_funds().to_dict()}

    def _ensure_funds(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        # The settlement currency is the plugin's to name, not the framework's to assume; how long
        # a request may stay open is the framework's to set, not the plugin's.
        currency = str(arguments.get("currency") or runtime.plugin.account_funds().currency)
        result = runtime.plugin.ensure_funds(
            float(arguments["amount"]),
            currency,
            reason=str(arguments.get("reason", "")),
            allow_pending=bool(arguments.get("allow_pending", True)),
            timeout_seconds=FUNDING_TIMEOUT_SECONDS,
            consult=self._consultation,
        )
        return {"platform": runtime.plugin.name, **result.to_dict()}

    def _note_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Let the round say what became of an operator's instruction, at the moment it happens.

        The twice-a-cycle review asks about all of them at once, which is the right place to notice
        a deadline has passed, and the wrong place to notice a trade just satisfied one: by then the
        round that made the trade is over and the evidence is a ledger row somebody has to read back.
        Whoever did the thing is in the best position to say it was done.

        Only what is open on this platform can be touched. A closed instruction stays closed because
        reopening one is a decision about what the operator is still owed, and an id from another
        platform is a mistake, not a shortcut.
        """
        if self._memory is None:
            return {"recorded": False, "why": "this round has no record to write to"}
        identifier = int(arguments["id"])
        state = str(arguments.get("state", "progress")).strip().lower()
        why = str(arguments.get("why", "")).strip()
        if state not in ("progress", "done", "expired"):
            raise ValueError("state must be progress, done or expired")
        # A state change nobody can check is worse than no state change: the record then says the
        # operator was served without saying how, and the next round reads that as settled. So the
        # reason has to be long enough to carry what was asked against what has happened, and a
        # bare "done" is refused here rather than stored and believed.
        if len(why) < 8:
            raise ValueError(
                "`why` must say what was asked, what has happened and what is left - e.g. "
                "\"asked for 5 USDT here, 3 bought, 2 to go\". A bare verdict is not a reason."
            )
        open_items = {item["id"]: item for item in self._memory.active_instructions(self._current)}
        if identifier not in open_items:
            return {
                "recorded": False,
                "why": f"instruction {identifier} is not open on {self._current}",
                "open_instructions": sorted(open_items),
            }
        if state == "progress":
            self._memory.note_instruction_progress(identifier, why)
        else:
            self._memory.resolve_instruction(identifier, status=state, resolution=why)
        return {
            "recorded": True,
            "id": identifier,
            "state": state,
            "still_binding": state == "progress",
            "asked": open_items[identifier]["instruction"],
        }

    def _list_instructions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            return {"instructions": [], "why": "this round has no record to read from"}
        platform = str(arguments.get("platform") or self._current)
        if bool(arguments.get("include_closed", False)):
            rows = [
                item for item in self._memory.instructions(limit=100)
                if item["platform"] in ("", platform)
            ]
            items = [
                {**OperatorInstructions.catalogue_entry(item), "status": item["status"],
                 "closed_because": item["resolution"]}
                for item in rows
            ]
        else:
            items = [
                OperatorInstructions.catalogue_entry(item)
                for item in by_urgency(self._memory.active_instructions(platform))
            ]
        return {"platform": platform, "instructions": items, "count": len(items)}

    def _read_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            return {"why": "this round has no record to read from"}
        found = instruction_detail(self._memory, int(arguments["id"]))
        return found or {"why": f"no instruction {arguments['id']} on record"}

    def _funding_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        result = runtime.plugin.funding_status(str(arguments["request_id"]))
        return {"platform": runtime.plugin.name, **result.to_dict()}

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
                    "funds": runtime.plugin.account_funds().to_dict(),
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

    def _list_topics_by_deadline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runtime = self._runtime(arguments)
        listing = getattr(runtime.plugin, "list_topics_by_deadline", None)
        if not callable(listing):
            return {"platform": runtime.plugin.name, "supported": False,
                    "note": "这个平台不能按结算时间列出标的，请用 LIST_TOPICS"}
        hours = float(arguments["within_hours"])
        if hours <= 0:
            raise ValueError("within_hours must be positive")
        now_ms = int(time.time() * 1000)
        page = listing(
            offset=int(arguments.get("offset", 0) or 0),
            limit=int(arguments.get("limit", 20) or 20),
            after_ms=now_ms,
            before_ms=now_ms + int(hours * 3600 * 1000),
        )
        return _plain({"platform": runtime.plugin.name, "supported": True,
                       "within_hours": hours, "page": page})

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
