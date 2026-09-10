from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any


RATE_LIMIT_PATTERNS = (
    r"\b429\b",
    r"rate[ _-]?limit",
    r"too many requests",
    r"quota",
    r"usage limit",
    r"capacity",
    r"overloaded",
    r"try again later",
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
COOLDOWN_CEILING = 3600


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
    attempts: int = 0
    successes: int = 0
    failures_by_kind: dict[str, int] = field(default_factory=dict)
    latency_total: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def average_latency(self) -> float:
        return self.latency_total / self.successes if self.successes else 0.0

    def available_at(self, now: float) -> bool:
        return now >= self.cooldown_until

    def manifest(self, now: float) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available_at(now),
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
            state.latency_total += max(0.0, latency_seconds)

    def record_failure(self, name: str, message: str) -> str:
        """Cool a provider down for a window matched to why it failed; returns the failure kind."""
        kind = classify_error(message)
        with self._lock:
            state = self._states.setdefault(name, ProviderState(name))
            state.attempts += 1
            state.consecutive_failures += 1
            state.last_error = message
            state.last_error_kind = kind
            state.failures_by_kind[kind] = state.failures_by_kind.get(kind, 0) + 1
            base = COOLDOWN_SECONDS.get(kind, COOLDOWN_SECONDS["unknown"])
            backoff = min(
                COOLDOWN_CEILING, base * (2 ** (state.consecutive_failures - 1))
            )
            state.cooldown_until = time.time() + backoff
        return kind

    def manifest(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [
                {**state.manifest(now), "quality": round(self._quality.get(name, 1.0), 4)}
                for name, state in sorted(self._states.items())
            ]
