"""A standing request to move money, and the operator's approval of it.

The framework may ask for an amount to be available at any point in a decision. Moving real money
on that ask alone would mean funds leaving a wallet with nobody watching, at a moment nobody chose,
for a reason that only exists inside one model's reasoning. So the ask becomes a request the
operator sees and approves, and the transfer happens on the approval rather than on the ask.

Only one request stands at a time. A robot that asked for 50 and then for 200 does not want 250,
and an operator reading a queue of superseded asks would be approving history. Which of the two
should be the one standing is not this module's to decide, though: it is a statement about what the
robot currently intends, and guessing "the newest" is right often enough to be dangerous - the
second ask may be a smaller position on a different market that should not cancel a transfer
already worth approving. So when a second ask arrives the asker is asked, in the wording below, and
the rule only applies when there is nobody to ask.

What approval means differs by venue and belongs to the plugin: one that can move money itself
transfers on approval, while one that cannot asks the operator to confirm they moved it and then
checks whether the balance actually says so.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent.consultation import AgentConsult, ConsultAnswer, ConsultOption


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
        """File a new request as the one waiting.

        Anything still pending is retired first, but not silently: `resolve_conflict` has already
        established that it should be, and writing over a live request here without that would
        discard an approval the operator may have been about to give.
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

    def cancel_pending(self, detail: str) -> dict[str, Any] | None:
        """Retire the waiting request without an operator answering it.

        Filed as refused rather than deleted, and with the reason it ended: a caller still holding
        that id has to be able to learn the request is dead and why, and finding nothing would leave
        it waiting on an approval that is never coming.
        """
        request = self._read().get("pending")
        if not isinstance(request, dict):
            return None
        return self.settle(
            state="refused",
            available=float(request.get("available_when_asked", 0) or 0),
            detail=detail,
        )

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


KEEP_NEW = 1
KEEP_OLD = 2
DROP_BOTH = 3
USE_NEW_AMOUNT = 4

CONFLICT_OPTIONS = (
    ConsultOption(KEEP_NEW, "Use this new request and cancel the one still waiting."),
    ConsultOption(KEEP_OLD, "Drop this new request; the one still waiting is the one you want."),
    ConsultOption(
        DROP_BOTH,
        "Cancel both. You no longer want either amount, and nothing should be waiting for approval.",
    ),
    ConsultOption(
        USE_NEW_AMOUNT,
        "Neither amount is right. Replace both with one request for the amount you give below.",
        fields={
            "amount": (
                "number: the single amount you actually want available, replacing both the waiting "
                "request and the one you just made"
            ),
            "reason": (
                "string: why that amount, shown to whoever approves it in place of your earlier "
                "wording"
            ),
        },
    ),
)
"""The four ways two competing asks can end, as this plugin will act on them.

Three of them are about which ask survives. The fourth exists because the answer to "these two
cannot both stand" is often neither: a robot that asked for 50, looked further and asked for 200 may
on reflection want 120, and forcing it to pick one of two amounts it no longer believes in would
either park a transfer that is wrong or throw away the one chance to correct it.
"""


def conflict_question(pending: dict[str, Any], amount: float, currency: str, reason: str) -> str:
    """State the fork in the numbers that decide it, without steering the answer.

    Both asks are quoted with the robot's own stated reasons, because the choice turns on whether
    the older reason still holds, and how long the older one has been waiting, because an ask that
    has been in front of the operator for a while may be about to be approved.
    """
    waited = max(0, int(time.time()) - int(pending.get("requested_at", 0) or 0))
    return (
        "You have asked for funds on this platform twice, and only one request can be waiting for "
        "approval at a time.\n\n"
        f"STILL_WAITING: {float(pending.get('amount', 0)):.6f} "
        f"{pending.get('currency', '')}, asked {waited // 60} minutes ago, your reason was: "
        f"{pending.get('reason', '')}\n"
        f"JUST_ASKED: {float(amount):.6f} {currency}, your reason is: {reason}\n\n"
        "Nobody has approved or refused the waiting one yet; whichever survives is what the "
        "operator will be shown, and cancelling it means no money moves for it."
    )


@dataclass(frozen=True)
class Resolution:
    """What to do with the two asks, once the asker has said.

    `settled_by_asker` separates "you chose to stop wanting this" from "this failed", which the
    caller has to be able to tell apart: one is a decision to be reported back plainly, the other is
    a problem to work around.
    """

    proceed: bool
    amount: float
    reason: str
    detail: str
    answer: ConsultAnswer | None = None

    @property
    def settled_by_asker(self) -> bool:
        return self.answer is not None and self.answer.ok


def resolve_conflict(
    requests: "FundingRequests",
    *,
    amount: float,
    currency: str,
    reason: str,
    consult: AgentConsult | None,
) -> Resolution:
    """Settle a second ask against an unanswered first one, asking the asker where possible.

    With nobody to ask - a panel action, a sweep, a test - the newest ask wins, which is the only
    rule that cannot leave the operator looking at an intention the robot has already moved past.
    When the asker cannot produce a usable answer the waiting request is left exactly as it is and
    the new ask is refused: the waiting one may be seconds from approval, and discarding it on a
    failed question would destroy something real to make room for something unconfirmed.
    """
    pending = requests.pending()
    if pending is None:
        return Resolution(proceed=True, amount=amount, reason=reason, detail="")
    if consult is None:
        requests.cancel_pending("Superseded by a later request")
        return Resolution(
            proceed=True, amount=amount, reason=reason,
            detail="An earlier request was still waiting and has been superseded by this one.",
        )
    answer = consult.ask(
        conflict_question(pending, amount, currency, reason),
        CONFLICT_OPTIONS,
        subject="funding request already waiting",
    )
    if not answer.ok:
        return Resolution(
            proceed=False, amount=amount, reason=reason, answer=answer,
            detail=(
                f"A request for {float(pending['amount']):.6f} {pending.get('currency', '')} is "
                "still waiting for approval and only one can wait at a time. You could not be "
                f"asked which should stand ({answer.error}), so the waiting one was left alone and "
                "this ask was not recorded. Check FUNDING_STATUS on "
                f"{pending['request_id']}, or ask again once it has settled."
            ),
        )
    if answer.chose(KEEP_OLD):
        return Resolution(
            proceed=False, amount=amount, reason=reason, answer=answer,
            detail=(
                "You chose to keep the request already waiting, so this ask was not recorded. Its "
                f"request_id is {pending['request_id']}; check FUNDING_STATUS on that one."
            ),
        )
    if answer.chose(DROP_BOTH):
        requests.cancel_pending(f"Cancelled by the asker: {answer.reason}")
        return Resolution(
            proceed=False, amount=amount, reason=reason, answer=answer,
            detail=(
                "You cancelled both requests, so nothing is waiting for approval and no money "
                "will move. Ask again if you change your mind."
            ),
        )
    if answer.chose(USE_NEW_AMOUNT):
        requests.cancel_pending(f"Replaced by a corrected amount: {answer.reason}")
        return Resolution(
            proceed=True,
            amount=float(answer.values["amount"]),
            reason=str(answer.values["reason"]),
            answer=answer,
            detail=(
                "You replaced both earlier amounts with this one; the waiting request was "
                "cancelled and the operator sees only this."
            ),
        )
    requests.cancel_pending(f"Superseded at the asker's request: {answer.reason}")
    return Resolution(
        proceed=True, amount=amount, reason=reason, answer=answer,
        detail="You cancelled the request that was waiting in favour of this one.",
    )


def shortfall_message(request: dict[str, Any], available: float) -> str:
    """Why a confirmation was not accepted, in the numbers the operator can act on."""
    missing = float(request.get("amount", 0)) - available
    return (
        f"The balance reads {available:.6f} {request.get('currency', '')}, short of the "
        f"{float(request.get('amount', 0)):.6f} requested by {missing:.6f}. "
        "A transfer can take time to settle; if you have sent it, wait and confirm again."
    )
