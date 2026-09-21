from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core.domain import AccountState


def _state(path: Path) -> AccountState | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return AccountState.from_dict(payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _metadata(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sum_known(values: list[float | None]) -> float | None:
    return round(sum(float(value) for value in values), 6) if all(
        value is not None for value in values
    ) else None


def _event_accounts(connection: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT platform, account_mode, currency, MIN(created_at), MAX(created_at), COUNT(*),
               COALESCE(SUM(fee), 0), COALESCE(SUM(external_flow_delta), 0),
               SUM(CASE WHEN evidence_status != 'complete' THEN 1 ELSE 0 END),
               SUM(CASE WHEN evidence_status != 'complete'
                         AND event_type != 'ACCOUNT_BASELINE' THEN 1 ELSE 0 END)
        FROM pnl_events
        GROUP BY platform, account_mode, currency
        """
    ).fetchall()
    accounts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row[0]), str(row[1]), str(row[2]))
        latest = connection.execute(
            """
            SELECT cash_after, equity_after, realized_pnl_after, metadata_json
            FROM pnl_events
            WHERE platform = ? AND account_mode = ? AND currency = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            key,
        ).fetchone()
        baseline = connection.execute(
            """
            SELECT evidence_status, metadata_json FROM pnl_events
            WHERE platform = ? AND account_mode = ? AND currency = ?
              AND event_type = 'ACCOUNT_BASELINE'
            ORDER BY id ASC LIMIT 1
            """,
            key,
        ).fetchone()
        known_realized = None if latest is None or latest[2] is None else round(float(latest[2]), 6)
        uncertain_pnl_events = int(row[9] or 0)
        accounts[key] = {
            "platform": key[0],
            "account_mode": key[1],
            "currency": key[2],
            "coverage_started_at": int(row[3]),
            "last_event_at": int(row[4]),
            "event_count": int(row[5]),
            "fees": round(float(row[6] or 0), 6),
            "net_external_flow": round(float(row[7] or 0), 6),
            "incomplete_events": int(row[8] or 0),
            "cash": None if latest is None or latest[0] is None else round(float(latest[0]), 6),
            "equity": None if latest is None or latest[1] is None else round(float(latest[1]), 6),
            "realized_pnl": None if uncertain_pnl_events else known_realized,
            "known_realized_pnl": known_realized,
            "baseline": _metadata(baseline[1]) if baseline else {},
            "historical_breakdown_complete": bool(
                baseline
                and str(baseline[0]) == "complete"
                and _metadata(baseline[1]).get("historical_breakdown_available", True)
            ),
            "uncertain_pnl_events": uncertain_pnl_events,
        }
    return accounts


def pnl_summary(
    session_db: Path,
    state_files: dict[str, Path],
    *,
    current_mode: str,
    platform: str = "",
    account_mode: str = "",
    currency: str = "",
) -> dict[str, Any]:
    """Return account P&L without combining unlike currencies or simulation modes."""
    connection = sqlite3.connect(session_db)
    try:
        event_accounts = _event_accounts(connection)
    finally:
        connection.close()

    accounts: dict[tuple[str, str, str], dict[str, Any]] = dict(event_accounts)
    for name, path in state_files.items():
        state = _state(path)
        if state is None:
            continue
        candidates = [key for key in event_accounts if key[0] == name and key[1] == current_mode]
        if candidates:
            key = max(candidates, key=lambda item: event_accounts[item]["last_event_at"])
        else:
            key = (name, current_mode, "UNKNOWN")
        marks_known = all(position.marked_at > 0 for position in state.positions.values())
        known_unrealized = sum(
            position.market_value - position.cost_basis
            for position in state.positions.values()
            if position.marked_at > 0
        )
        unrealized = round(known_unrealized, 6) if marks_known else None
        event = accounts.get(key, {})
        order_fees = sum(
            order.fee for order in state.orders if str(order.status).upper() == "FILLED"
        )
        baseline = event.get("baseline", {})
        uncertain_pnl_events = int(event.get("uncertain_pnl_events", 0))
        realized = None if uncertain_pnl_events else round(state.realized_pnl, 6)
        accounts[key] = {
            **event,
            "platform": name,
            "account_mode": current_mode,
            "currency": key[2],
            "current": True,
            "starting_capital": round(state.starting_capital, 6),
            "cash": round(state.cash, 6),
            "exposure": round(state.exposure, 6),
            "equity": round(state.equity, 6),
            "realized_pnl": realized,
            "known_realized_pnl": round(state.realized_pnl, 6),
            "unrealized_pnl": unrealized,
            "known_unrealized_pnl": round(known_unrealized, 6),
            "total_pnl": (
                None if unrealized is None or realized is None else round(realized + unrealized, 6)
            ),
            "fees": round(max(float(event.get("fees", 0)), order_fees), 6),
            "net_external_flow": round(float(event.get("net_external_flow", 0)), 6),
            "open_positions": len(state.positions),
            "positions_without_current_mark": sum(
                position.marked_at <= 0 for position in state.positions.values()
            ),
            "valuation_observed_at": (
                min(position.marked_at for position in state.positions.values())
                if state.positions and marks_known else None
            ),
            "state_updated_at": state.updated_at,
            "coverage_started_at": event.get("coverage_started_at", state.created_at),
            "last_event_at": event.get("last_event_at"),
            "event_count": int(event.get("event_count", 0)),
            "incomplete_events": int(event.get("incomplete_events", 0)),
            "uncertain_pnl_events": uncertain_pnl_events,
            "historical_breakdown_complete": bool(
                event.get("historical_breakdown_complete", False)
            ),
            "basis": "local_account_mirror_and_immutable_events",
        }

    rows = []
    for item in accounts.values():
        item = {
            "current": False,
            "starting_capital": item.get("baseline", {}).get("starting_capital"),
            "exposure": None,
            "unrealized_pnl": None,
            "known_unrealized_pnl": None,
            "known_realized_pnl": item.get("known_realized_pnl"),
            "total_pnl": None,
            "open_positions": None,
            "positions_without_current_mark": None,
            "valuation_observed_at": None,
            "state_updated_at": None,
            "basis": "immutable_events_only",
            **item,
        }
        item.pop("baseline", None)
        if platform and item["platform"] != platform:
            continue
        if account_mode and item["account_mode"] != account_mode.lower():
            continue
        if currency and item["currency"] != currency.upper():
            continue
        rows.append(item)
    rows.sort(key=lambda item: (not item["current"], item["platform"], item["currency"]))

    totals = []
    groups = sorted({(item["account_mode"], item["currency"]) for item in rows})
    for mode, unit in groups:
        grouped = [item for item in rows if item["account_mode"] == mode and item["currency"] == unit]
        totals.append(
            {
                "account_mode": mode,
                "currency": unit,
                "accounts": len(grouped),
                "equity": _sum_known([item.get("equity") for item in grouped]),
                "realized_pnl": _sum_known([item.get("realized_pnl") for item in grouped]),
                "unrealized_pnl": _sum_known([item.get("unrealized_pnl") for item in grouped]),
                "total_pnl": _sum_known([item.get("total_pnl") for item in grouped]),
                "fees": round(sum(float(item.get("fees") or 0) for item in grouped), 6),
                "net_external_flow": round(
                    sum(float(item.get("net_external_flow") or 0) for item in grouped), 6
                ),
                "complete": all(
                    item.get("equity") is not None
                    and item.get("realized_pnl") is not None
                    and item.get("unrealized_pnl") is not None
                    and item.get("historical_breakdown_complete") is True
                    and int(item.get("incomplete_events") or 0) == 0
                    for item in grouped
                ),
            }
        )
    return {
        "accounts": rows,
        "totals": totals,
        "account_modes": sorted({item["account_mode"] for item in rows}),
        "currencies": sorted({item["currency"] for item in rows}),
        "platforms": sorted({item["platform"] for item in rows}),
        "accounting_note": (
            "Transfers change net external flow, never profit. Unknown cost basis or marks stay null. "
            "Totals never combine currencies or live and paper accounts."
        ),
    }


def pnl_positions(
    state_files: dict[str, Path],
    *,
    current_mode: str,
    platform: str = "",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for name, path in state_files.items():
        if platform and name != platform:
            continue
        state = _state(path)
        if state is None:
            continue
        for position in state.positions.values():
            known = position.marked_at > 0
            rows.append(
                {
                    "platform": name,
                    "account_mode": current_mode,
                    "token_id": position.token_id,
                    "market_topic_id": str(position.market_topic_id),
                    "market_id": str(position.market_id),
                    "symbol": position.symbol,
                    "direction": position.direction,
                    "quantity": round(position.quantity, 8),
                    "average_price": round(position.average_price, 8),
                    "mark_price": round(position.mark_price, 8) if known else None,
                    "cost_basis": round(position.cost_basis, 6),
                    "market_value": round(position.market_value, 6) if known else None,
                    "unrealized_pnl": (
                        round(position.market_value - position.cost_basis, 6) if known else None
                    ),
                    "opened_at": position.opened_at,
                    "marked_at": position.marked_at or None,
                    "evidence_status": "complete" if known else "mark_unknown",
                }
            )
    rows.sort(key=lambda item: (item["platform"], -int(item["opened_at"])))
    return {"items": rows, "count": len(rows)}


def pnl_events(
    session_db: Path,
    *,
    limit: int,
    offset: int,
    platform: str = "",
    account_mode: str = "",
    currency: str = "",
    event_type: str = "",
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("platform", platform),
        ("account_mode", account_mode.lower()),
        ("currency", currency.upper()),
        ("event_type", event_type.upper()),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = sqlite3.connect(session_db)
    try:
        total = int(connection.execute(f"SELECT COUNT(*) FROM pnl_events{where}", values).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT id, created_at, platform, account_mode, currency, event_type,
                   market_topic_id, market_id, token_id, order_id, decision_id,
                   quantity, price, cash_delta, position_quantity_delta, cost_basis_delta,
                   realized_pnl_delta, fee, external_flow_delta, cash_after, equity_after,
                   realized_pnl_after, evidence_status, metadata_json
            FROM pnl_events{where}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            [*values, int(limit), int(offset)],
        ).fetchall()
        types = [
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT event_type FROM pnl_events{where} ORDER BY event_type", values
            ).fetchall()
        ]
    finally:
        connection.close()
    names = (
        "id", "created_at", "platform", "account_mode", "currency", "event_type",
        "market_topic_id", "market_id", "token_id", "order_id", "decision_id",
        "quantity", "price", "cash_delta", "position_quantity_delta", "cost_basis_delta",
        "realized_pnl_delta", "fee", "external_flow_delta", "cash_after", "equity_after",
        "realized_pnl_after", "evidence_status", "metadata",
    )
    items = []
    for row in rows:
        item = dict(zip(names, row))
        item["metadata"] = _metadata(str(item["metadata"] or "{}"))
        items.append(item)
    return {"items": items, "count": total, "limit": limit, "offset": offset, "event_types": types}
