from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RATE_LIMIT_PATTERNS = (
    r"\b429\b",
    r"rate[ _-]?limit",
    r"too many requests",
    r"quota",
    r"usage limit",
    # "session limit" is the same fact under another name, and classifying it as unknown gave it a
    # minute of cooldown instead of the hours it actually needs - so the provider was retried all
    # the way through an outage it had already stated the end time of.
    r"session limit",
    # The same again for every other window a subscription is metered in. "weekly limit" was left
    # unknown, so through a week-long lockout the provider came back every minute and each round
    # left another failed record in the ledger - 461 of them before anyone noticed.
    r"hit your [\w -]*limit",
)
AUTH_PATTERNS = (
    r"\b401\b",
    r"\b403\b",
    r"unauthor",
    r"forbidden",
    r"invalid[ _-]?(api[ _-]?)?key",
    r"expired",
    r"not logged in",
    r"credential",
    r"authenticat",
)
TRANSIENT_PATTERNS = (
    r"\b5\d\d\b",
    r"timed? ?out",
    r"timeout",
    r"connection",
    r"temporarily",
    r"unavailable",
    r"reset by peer",
    r"capacity",
    r"overloaded",
    r"try again later",
)
CONTRACT_PATTERNS = (
    r"schema",
    r"json",
    r"invalid decision",
    r"unsupported action",
    r"unsupported order type",
    r"outside \[0, 1\]",
    r"parse",
)

COOLDOWN_SECONDS = {
    "rate_limit": 300,
    "auth": 900,
    "transient": 30,
    "contract": 15,
    "unknown": 60,
}
COOLDOWN_CEILING = 900
"""Longest a provider waits between checks when nothing says how long its outage lasts.

A provider that states when its limit lifts is taken at its word instead: every round started
before then is market data pulled, a prompt built and a failed record written, for an answer that
was never coming. That is what ran up hundreds of failed records through one weekly limit. The
operator can still change plan, buy credits or swap account early - which is what the recheck
action is for, and it puts a provider back in line at once.
"""

RESET_HORIZON_SECONDS = 31 * 86400
"""A stated reset further out than any subscription window is a misread, not a plan."""

PROBE_INTERVAL_SECONDS = 300
"""Shortest gap between two out-of-band liveness probes of the same provider."""

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_RESET_ANCHOR = re.compile(
    r"(?:resets?|try again at|available again at|until)\s*(?:on\s+|at\s+)?", re.IGNORECASE
)
_RESET_MOMENT = re.compile(
    r"(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(?P<year>\d{4}),?\s*)?(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?\s*m\b\.?"
    r"(?:\s*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)
_RESET_AFTER = re.compile(
    r"(?:try again|retry|resets?)\s+(?:in|after)\s+(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|[smhd])\b",
    re.IGNORECASE,
)


def parse_reset_time(text: str, now: float | None = None) -> float | None:
    """When a provider said its limit lifts, as a timestamp; None when it did not say.

    Clients say it in their own words - "resets 12:20pm (UTC)", "resets Sep 21, 1pm (UTC)",
    "try again at Sep 19th, 2026 8:13 AM", "try again in 20 minutes". A time without a date is its
    next occurrence; a time without a zone is this machine's, because the client that printed it
    runs here. Anything already past or implausibly far away is not an answer.
    """
    moment = time.time() if now is None else float(now)
    source = str(text or "")
    after = _RESET_AFTER.search(source)
    if after:
        unit = after.group("unit").lower()[0]
        seconds = float(after.group("amount")) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return moment + seconds if 0 < seconds <= RESET_HORIZON_SECONDS else None
    for anchor in _RESET_ANCHOR.finditer(source):
        found = _RESET_MOMENT.match(source, anchor.end())
        if not found:
            continue
        zone = _zone(found.group("zone"))
        hour = int(found.group("hour")) % 12 + (12 if found.group("half").lower() == "p" else 0)
        minute = int(found.group("minute") or 0)
        if hour > 23 or minute > 59:
            continue
        today = datetime.fromtimestamp(moment, zone)
        try:
            if found.group("month"):
                month = _MONTHS.index(found.group("month").lower()[:3]) + 1
                year = int(found.group("year") or today.year)
                when = datetime(year, month, int(found.group("day")), hour, minute, tzinfo=zone)
                if not found.group("year") and when.timestamp() < moment - 86400:
                    when = when.replace(year=year + 1)
            else:
                when = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if when.timestamp() <= moment:
                    when += timedelta(days=1)
        except ValueError:
            continue
        stamp = when.timestamp()
        return stamp if moment < stamp <= moment + RESET_HORIZON_SECONDS else None
    return None


def _zone(name: str | None) -> tzinfo | None:
    label = (name or "").strip()
    if not label:
        return None
    if label.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc
    try:
        return ZoneInfo(label)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def classify_error(message: str) -> str:
    """Name the failure so recovery can be paced to what actually went wrong.

    A rate limit needs minutes of quiet; a dropped connection needs seconds. Retrying both at the
    same cadence either hammers a throttled account or idles a healthy one.
    """
    text = (message or "").casefold()
    for kind, patterns in (
        ("rate_limit", RATE_LIMIT_PATTERNS),
        ("auth", AUTH_PATTERNS),
        ("contract", CONTRACT_PATTERNS),
        ("transient", TRANSIENT_PATTERNS),
    ):
        if any(re.search(pattern, text) for pattern in patterns):
            return kind
    return "unknown"


@dataclass
class ProviderState:
    """Live health of one decision provider within this process."""

    name: str
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_error_kind: str = ""
    last_success_at: float = 0.0
    probation: bool = False
    last_probe_at: float = 0.0
    attempts: int = 0
    successes: int = 0
    failures_by_kind: dict[str, int] = field(default_factory=dict)
    latency_total: float = 0.0
    recovers_at: float = 0.0
    """When the provider itself said it can answer again; 0 when it did not say."""

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def average_latency(self) -> float:
        return self.latency_total / self.successes if self.successes else 0.0

    def available_at(self, now: float) -> bool:
        return now >= self.cooldown_until

    def ready(self, now: float) -> bool:
        """Whether a decision may be put to this provider now.

        A lapsed cooldown only says the window may have reopened. Until a check made away from the
        decision path confirms it, a round started on the strength of it would collect market data
        and write a record for an answer that may still not come.
        """
        return self.available_at(now) and not self.probation

    def needs_probe(self, now: float) -> bool:
        """Whether this provider should be tried out of band rather than on a decision.

        A failed probe can be slow - a client may spend half a minute retrying its transport before
        reporting the refusal - so a provider that has not proved itself since its last failure is
        checked away from the decision path, not on it.
        """
        return (
            self.probation
            and self.available_at(now)
            and now - self.last_probe_at >= PROBE_INTERVAL_SECONDS
        )

    def manifest(self, now: float) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available_at(now),
            "probation": self.probation,
            "ready": self.ready(now),
            "recovers_at": int(self.recovers_at),
            "cooldown_seconds_remaining": max(0, round(self.cooldown_until - now)),
            "consecutive_failures": self.consecutive_failures,
            "attempts": self.attempts,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 4),
            "average_latency_seconds": round(self.average_latency, 3),
            "last_error_kind": self.last_error_kind,
            "last_error": self.last_error[:300],
            "last_success_at": int(self.last_success_at),
            "failures_by_kind": dict(self.failures_by_kind),
        }


class ProviderHealthRegistry:
    """Track which providers are usable right now and how well each has been performing.

    Providers fail for reasons that resolve on their own: a quota window closes, a session expires
    and is renewed, an endpoint sheds load. The registry keeps a failing provider out of the
    rotation for as long as its failure kind warrants, then lets exactly one request through to
    find out whether it recovered.
    """

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, ProviderState] = {
            name: ProviderState(name) for name in names
        }
        self._quality: dict[str, float] = {}

    def state(self, name: str) -> ProviderState:
        with self._lock:
            return self._states.setdefault(name, ProviderState(name))

    def set_quality(self, scores: dict[str, float]) -> None:
        """Install measured quality scores used to order otherwise-available providers."""
        with self._lock:
            self._quality = {str(key): float(value) for key, value in scores.items()}

    def quality(self, name: str) -> float:
        with self._lock:
            return self._quality.get(name, 1.0)

    def order(self, names: tuple[str, ...], *, now: float | None = None) -> list[str]:
        """Available providers first, best measured quality first, configured order as tiebreak.

        A cooled-down provider is not dropped: it moves to the back, so a run where every provider
        is throttled still tries them rather than refusing to decide.
        """
        moment = time.time() if now is None else now
        with self._lock:
            ranked = sorted(
                enumerate(names),
                key=lambda item: (
                    not self._states.setdefault(
                        item[1], ProviderState(item[1])
                    ).available_at(moment),
                    # Not yet proved since its last failure: usable, but not ahead of a provider
                    # that is currently working.
                    self._states[item[1]].probation,
                    -self._quality.get(item[1], 1.0),
                    item[0],
                ),
            )
            return [name for _, name in ranked]

    def record_success(self, name: str, *, latency_seconds: float = 0.0) -> None:
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name))
            state.attempts += 1
            state.successes += 1
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            state.last_error = ""
            state.last_error_kind = ""
            state.last_success_at = time.time()
            state.probation = False
            state.recovers_at = 0.0
            state.latency_total += max(0.0, latency_seconds)

    def record_probe_success(self, name: str) -> None:
        """The client says it can serve again - which is not the same as having served.

        Only a real answer resets the failure streak, so a client whose own report disagrees with
        what its requests keep doing is paced by a growing backoff rather than trusted afresh each
        time it says so.
        """
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name))
            state.cooldown_until = 0.0
            state.probation = False
            state.recovers_at = 0.0
            state.last_error = ""
            state.last_error_kind = ""

    def record_failure(
        self,
        name: str,
        message: str,
        *,
        kind: str | None = None,
        recovers_at: float | None = None,
        attempted: bool = True,
    ) -> str:
        """Cool a provider down for a window matched to why it failed; returns the failure kind.

        `attempted` is False when nothing was asked of the provider - its client reported, for
        free, that it cannot serve - so the measured success rate is left alone.
        """
        kind = kind or classify_error(message)
        now = time.time()
        stated = recovers_at if recovers_at and recovers_at > now else None
        if stated is None and kind == "rate_limit":
            stated = parse_reset_time(message, now)
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name))
            if attempted:
                state.attempts += 1
                state.failures_by_kind[kind] = state.failures_by_kind.get(kind, 0) + 1
            state.consecutive_failures += 1
            state.last_error = message
            state.last_error_kind = kind
            state.probation = True
            base = COOLDOWN_SECONDS.get(kind, COOLDOWN_SECONDS["unknown"])
            backoff = min(
                COOLDOWN_CEILING, base * (2 ** (state.consecutive_failures - 1))
            )
            state.recovers_at = float(stated or 0.0)
            state.cooldown_until = float(stated) if stated else now + backoff
        return kind

    def due_for_probe(self, names: tuple[str, ...] = ()) -> list[str]:
        now = time.time()
        with self._lock:
            candidates = names or tuple(self._states)
            return [
                name
                for name in candidates
                if name in self._states and self._states[name].needs_probe(now)
            ]

    def record_probe(self, name: str) -> None:
        with self._lock:
            self._states.setdefault(name, ProviderState(name)).last_probe_at = time.time()

    def recheck(self, name: str = "") -> list[str]:
        """Put cooled-down providers back in line right now.

        Entitlement changes out of band: a plan changes, credits are bought, an account is swapped.
        The operator knows when that happened and the runtime cannot, so this exists to be pressed.
        """
        with self._lock:
            targets = [name] if name else list(self._states)
            cleared = []
            for target in targets:
                state = self._states.get(target)
                if state is None:
                    continue
                state.cooldown_until = 0.0
                state.consecutive_failures = 0
                state.last_probe_at = 0.0
                state.recovers_at = 0.0
                cleared.append(target)
            return cleared

    def manifest(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [
                {**state.manifest(now), "quality": round(self._quality.get(name, 1.0), 4)}
                for name, state in sorted(self._states.items())
            ]
