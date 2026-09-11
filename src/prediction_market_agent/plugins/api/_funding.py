"""A standing request to move money, and the operator's approval of it.

The framework may ask for an amount to be available at any point in a decision. Moving real money
on that ask alone would mean funds leaving a wallet with nobody watching, at a moment nobody chose,
for a reason that only exists inside one model's reasoning. So the ask becomes a request the
operator sees and approves, and the transfer happens on the approval rather than on the ask.

Only the latest request stands. A robot that asked for 50 and then for 200 does not want 250; it
wants 200, and an operator reading a queue of superseded asks would be approving history.

What approval means differs by venue and belongs to the plugin: one that can move money itself
transfers on approval, while one that cannot asks the operator to confirm they moved it and then
checks whether the balance actually says so.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class FundingRequests:
    """One pending request, kept on disk so an approval survives a restart."""

    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) and value.get("amount") else None

    def record(self, *, amount: float, currency: str, available: float, reason: str) -> dict[str, Any]:
        """Replace whatever was pending. The newest ask is the only one that means anything."""
        request = {
            "amount": round(float(amount), 8),
            "currency": currency,
            "available_when_asked": round(float(available), 8),
            "shortfall": round(max(0.0, float(amount) - float(available)), 8),
            "reason": reason,
            "requested_at": int(time.time()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        return request

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def shortfall_message(request: dict[str, Any], available: float) -> str:
    """Why a confirmation was not accepted, in the numbers the operator can act on."""
    missing = float(request.get("amount", 0)) - available
    return (
        f"The balance reads {available:.6f} {request.get('currency', '')}, short of the "
        f"{float(request.get('amount', 0)):.6f} requested by {missing:.6f}. "
        "A transfer can take time to settle; if you have sent it, wait and confirm again."
    )
