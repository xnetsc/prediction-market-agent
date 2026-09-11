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
import uuid
from pathlib import Path
from typing import Any


class FundingRequests:
    """One outstanding request plus the outcome of recent ones, so a later answer can be matched.

    An ask that resolves after the call returns has to be findable again, and by the caller's own
    reference rather than by "the last one", because a second ask may have arrived in between.
    Outcomes are kept for a while after they settle: a caller that asks a moment too late should
    read what happened, not be told the request never existed.
    """

    HISTORY = 20

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pending": None, "settled": []}
        if not isinstance(value, dict):
            return {"pending": None, "settled": []}
        value.setdefault("pending", None)
        value.setdefault("settled", [])
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def pending(self) -> dict[str, Any] | None:
        """The live request, or nothing. A request past its deadline is expired here, not returned.

        Expiry is checked on read rather than on a timer: there is no loop to run one, and a
        request that quietly outlived its deadline would otherwise be approved long after the
        decision it was for had gone.
        """
        request = self._read()["pending"]
        if not (isinstance(request, dict) and request.get("amount")):
            return None
        expires_at = int(request.get("expires_at", 0) or 0)
        if expires_at and time.time() > expires_at:
            self.settle(
                state="failed",
                available=float(request.get("available_when_asked", 0)),
                detail="The request expired before it was answered",
            )
            return None
        return request

    def record(
        self,
        *,
        amount: float,
        currency: str,
        available: float,
        reason: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Replace whatever was pending. The newest ask is the only one that means anything.

        The one it replaces is not forgotten: it is filed as superseded, so a caller still holding
        that id learns why it will never complete instead of finding nothing.
        """
        book = self._read()
        previous = book.get("pending")
        if isinstance(previous, dict) and previous.get("request_id"):
            book["settled"] = (
                [{**previous, "state": "refused", "detail": "Superseded by a later request"}]
                + list(book.get("settled", []))
            )[: self.HISTORY]
        request = {
            "request_id": uuid.uuid4().hex,
            "state": "pending",
            "amount": round(float(amount), 8),
            "currency": currency,
            "available_when_asked": round(float(available), 8),
            "shortfall": round(max(0.0, float(amount) - float(available)), 8),
            "reason": reason,
            "requested_at": int(time.time()),
            "expires_at": int(time.time()) + int(timeout_seconds),
        }
        book["pending"] = request
        self._write(book)
        return request

    def settle(
        self, *, state: str, available: float, detail: str, operator_note: str = ""
    ) -> dict[str, Any] | None:
        """File the outcome of the pending request so it can be read back by its id."""
        book = self._read()
        request = book.get("pending")
        if not isinstance(request, dict):
            return None
        finished = {
            **request,
            "state": state,
            "available": round(float(available), 8),
            "detail": detail,
            "operator_note": operator_note,
            "settled_at": int(time.time()),
        }
        book["pending"] = None
        book["settled"] = ([finished] + list(book.get("settled", [])))[: self.HISTORY]
        self._write(book)
        return finished

    def find(self, request_id: str) -> dict[str, Any] | None:
        book = self._read()
        request = book.get("pending")
        if isinstance(request, dict) and request.get("request_id") == request_id:
            return request
        for item in book.get("settled", []):
            if isinstance(item, dict) and item.get("request_id") == request_id:
                return item
        return None

    def clear(self) -> None:
        self._write({"pending": None, "settled": self._read().get("settled", [])})


def shortfall_message(request: dict[str, Any], available: float) -> str:
    """Why a confirmation was not accepted, in the numbers the operator can act on."""
    missing = float(request.get("amount", 0)) - available
    return (
        f"The balance reads {available:.6f} {request.get('currency', '')}, short of the "
        f"{float(request.get('amount', 0)):.6f} requested by {missing:.6f}. "
        "A transfer can take time to settle; if you have sent it, wait and confirm again."
    )
