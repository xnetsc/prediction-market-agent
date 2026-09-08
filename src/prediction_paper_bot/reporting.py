from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .models import AccountState


def _prices(input_json: str) -> tuple[float, float] | None:
    try:
        book = json.loads(input_json)["order_book"]
        bid, ask = float(book["best_bid"]), float(book["best_ask"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return bid, ask


def _later_snapshot(
    observations: list[dict[str, Any]], platform: str, token_id: str, target_ms: int
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in observations
            if item["platform"] == platform
            and item["token_id"] == token_id
            and item["created_at"] >= target_ms
        ),
        None,
    )


def build_report(
    session_db: Path,
    state_file: Path | dict[str, Path],
    *,
    since_ms: int = 0,
    horizons_minutes: tuple[int, ...] = (15, 60, 240),
) -> dict[str, Any]:
    """Compare decisions with later tradable quotes and summarize account mirrors."""
    connection = sqlite3.connect(session_db)
    rows = connection.execute(
        """
        SELECT id, created_at, platform, provider, market_topic_id, token_id,
               input_json, decision_json, status, error
        FROM provider_turns
        WHERE created_at >= ?
        ORDER BY created_at, id
        """,
        (since_ms,),
    ).fetchall()
    step_rows = connection.execute(
        """
        SELECT provider, tool_name, status FROM agent_steps
        WHERE created_at >= ? ORDER BY created_at, id
        """,
        (since_ms,),
    ).fetchall()
    action_rows = connection.execute(
        """
        SELECT platform, action FROM execution_actions
        WHERE created_at >= ? ORDER BY created_at, id
        """,
        (since_ms,),
    ).fetchall()
    connection.close()

    observations: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    provider_errors = 0
    for row in rows:
        row_id, created_at, platform, provider, topic_id, token_id, input_json, decision_json, status, error = row
        quote = _prices(input_json)
        if quote is not None:
            observations.append(
                {
                    "id": row_id,
                    "created_at": created_at,
                    "platform": platform,
                    "token_id": token_id,
                    "bid": quote[0],
                    "ask": quote[1],
                    "mid": (quote[0] + quote[1]) / 2,
                }
            )
    for row in rows:
        row_id, created_at, platform, provider, topic_id, token_id, input_json, decision_json, status, error = row
        quote = _prices(input_json)
        if status != "OK" or not decision_json or quote is None:
            provider_errors += int(status != "OK")
            continue
        try:
            decision = json.loads(decision_json)
            market = json.loads(input_json).get("market", {})
        except json.JSONDecodeError:
            provider_errors += 1
            continue
        entry_bid, entry_ask = quote
        comparison: dict[str, Any] = {}
        for minutes in horizons_minutes:
            future = _later_snapshot(
                observations, platform, token_id, created_at + minutes * 60_000
            )
            if future is None:
                comparison[f"{minutes}m"] = None
                continue
            action = str(decision.get("action", "HOLD")).upper()
            tradable_edge = None
            if action == "BUY":
                tradable_edge = future["bid"] - entry_ask
            elif action == "SELL":
                tradable_edge = entry_bid - future["ask"]
            comparison[f"{minutes}m"] = {
                "observed_at": future["created_at"],
                "future_bid": future["bid"],
                "future_ask": future["ask"],
                "mid_change": round(future["mid"] - (entry_bid + entry_ask) / 2, 6),
                "tradable_edge_per_share": (
                    None if tradable_edge is None else round(tradable_edge, 6)
                ),
            }
        decisions.append(
            {
                "turn_id": row_id,
                "created_at": created_at,
                "provider": provider,
                "platform": platform,
                "market_topic_id": topic_id,
                "token_id": token_id,
                "title": market.get("title"),
                "market_title": market.get("market_title"),
                "action": decision.get("action"),
                "confidence": decision.get("confidence"),
                "estimated_probability": decision.get("estimated_probability"),
                "rationale": decision.get("rationale"),
                "entry_bid": entry_bid,
                "entry_ask": entry_ask,
                "later_quotes": comparison,
            }
        )

    state_files = state_file if isinstance(state_file, dict) else {"binance": state_file}
    accounts: dict[str, Any] = {}
    for platform, path in state_files.items():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            state = AccountState.from_dict(json.load(handle))
        accounts[platform] = {
            "state_file": str(path),
            "initial_balance": state.starting_capital,
            "cash": round(state.cash, 6),
            "exposure": round(state.exposure, 6),
            "equity": round(state.equity, 6),
            "realized_pnl": round(state.realized_pnl, 6),
            "risk_metrics": {
                key: round(value, 6) for key, value in sorted(state.risk_metrics.items())
            },
            "positions": len(state.positions),
            "orders": len(state.orders),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        }

    providers = Counter(item["provider"] for item in decisions)
    decision_actions = Counter(str(item["action"]) for item in decisions)
    tools = Counter(tool for _, tool, _ in step_rows if tool and tool != "FINAL_DECISION")
    step_statuses = Counter(status for _, _, status in step_rows)
    execution_actions = Counter(f"{platform}:{action}" for platform, action in action_rows)
    evaluated_edges = [
        horizon["tradable_edge_per_share"]
        for item in decisions
        for horizon in item["later_quotes"].values()
        if horizon is not None and horizon["tradable_edge_per_share"] is not None
    ]
    return {
        "session_db": str(session_db),
        "state_files": {name: str(path) for name, path in state_files.items()},
        "since_ms": since_ms,
        "summary": {
            "decisions": len(decisions),
            "provider_errors": provider_errors,
            "providers": dict(providers),
            "decision_actions": dict(decision_actions),
            "agent_steps": len(step_rows),
            "agent_step_statuses": dict(step_statuses),
            "research_tools": dict(tools),
            "execution_actions": dict(execution_actions),
            "evaluated_trade_horizons": len(evaluated_edges),
            "positive_trade_horizons": sum(value > 0 for value in evaluated_edges),
        },
        "accounts": accounts,
        "aggregate_account": {
            "equity": round(sum(item["equity"] for item in accounts.values()), 6),
            "risk_metrics": {
                key: round(
                    sum(
                        float(item["risk_metrics"].get(key, 0.0))
                        for item in accounts.values()
                    ),
                    6,
                )
                for key in sorted(
                    {
                        name
                        for item in accounts.values()
                        for name in item["risk_metrics"]
                    }
                )
            },
        },
        "decisions_detail": decisions,
    }
