"""Watching whether anything can still answer, and saying so to whoever schedules work.

Every cycle this robot runs exists to reach a decision, and a decision needs a model. When none
can answer - every provider rate limited, signed out, or unreachable - the scanning goes on
regardless: markets are pulled, candidates scored, prompts assembled, and the whole round is
thrown away at the last step. That costs the venues' rate budget and the operator's patience, and
it buries the one fact that matters, which is that the robot is not deciding anything.

So the fact gets its own loop. It is the only thing that should still be running when nothing can
answer, and it is deliberately cheap: it reads what the health registry already recorded from real
calls rather than spending a request of its own to find out.

What anyone does about it is not decided here. Plugins own their schedules - only they know what a
cycle of theirs is for - so this states the fact and they decide whether to stand down.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..agent.provider_health import PROBE_INTERVAL_SECONDS

LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
"""How often the fact is re-read. It is a local lookup, so this is about how quickly work resumes."""


def capacity_reading(provider: Any, now: float | None = None) -> dict[str, Any]:
    """Who can answer right now, and for those who cannot, what they are waiting out.

    Backoff is what makes this answerable without asking anything: a provider that failed had its
    failure classified and a retry time set, so being unavailable is already recorded and does not
    have to be rediscovered by failing again. `waiting` keeps the one-line reason plugins already
    read; `providers` carries what a person needs - why, and when it is expected back.
    """
    moment = time.time() if now is None else float(now)
    if getattr(provider, "available_names", None) is None:
        # A provider that keeps no health of its own has nothing recorded against it, so there is
        # no known reason to hold work back.
        return {"available": True, "ready": (), "waiting": {}, "providers": {}, "checked_at": int(moment)}
    names = tuple(provider.available_names or ())
    health = getattr(provider, "health", None)
    ready: list[str] = []
    waiting: dict[str, str] = {}
    providers: dict[str, dict[str, Any]] = {}
    for name in names:
        state = health.state(name) if health is not None else None
        if state is None or state.ready(moment):
            ready.append(name)
            providers[name] = {"ready": True}
            continue
        kind = str(getattr(state, "last_error_kind", "") or "unavailable")
        waiting[name] = kind
        providers[name] = {
            "ready": False,
            "kind": kind,
            "error": str(state.last_error)[:300],
            "recovers_at": int(state.recovers_at),
            # Past the cooldown and only waiting for a check to confirm it is back.
            "confirming": state.available_at(moment),
            "next_check_at": int(max(state.cooldown_until, state.last_probe_at + PROBE_INTERVAL_SECONDS)),
        }
    for name, reason in dict(getattr(provider, "unavailable", {}) or {}).items():
        waiting.setdefault(name, str(reason)[:200])
        providers.setdefault(name, {
            "ready": False, "kind": "unavailable", "error": str(reason)[:300],
            "recovers_at": 0, "confirming": False, "next_check_at": 0,
        })
    return {
        "available": bool(ready),
        "ready": tuple(ready),
        "waiting": waiting,
        "providers": providers,
        "checked_at": int(moment),
    }


class DecisionCapacityWatch:
    """Reports whether any decision provider is currently able to answer, and when that changes."""

    def __init__(
        self,
        provider: Any,
        listeners: Callable[[], list[tuple[str, Callable[[dict[str, Any]], None]]]],
        *,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
        probe: Callable[[], Any] | None = None,
    ) -> None:
        self._provider = provider
        self._listeners = listeners
        # Recovery has to be noticed by something that keeps running while the plugins stand down.
        # Waiting for a round to discover it would mean starting the very work this loop stops.
        self._probe = probe
        self._interval = max(5, int(interval_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"available": True, "ready": (), "waiting": {}, "checked_at": 0}

    def reading(self) -> dict[str, Any]:
        return capacity_reading(self._provider)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def check(self) -> dict[str, Any]:
        """Read it once and tell the listeners if the answer turned over."""
        if self._probe is not None:
            try:
                self._probe()
            except Exception:
                LOGGER.exception("decision capacity probe failed; reading what is recorded")
        reading = self.reading()
        with self._lock:
            changed = reading["available"] != self._state.get("available")
            self._state = reading
        if changed:
            LOGGER.info(
                "decision capacity is now %s (ready=%s waiting=%s)",
                "available" if reading["available"] else "unavailable",
                ",".join(reading["ready"]) or "none",
                ",".join(reading["waiting"]) or "none",
            )
            self._announce(reading)
        return reading

    def _announce(self, reading: dict[str, Any]) -> None:
        message = {
            "kind": "decision_capacity",
            "available": reading["available"],
            "ready": list(reading["ready"]),
            "waiting": dict(reading["waiting"]),
        }
        for name, notify in self._listeners():
            try:
                notify(message)
            except Exception:
                LOGGER.exception("%s failed while being told about decision capacity", name)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="prediction-decision-capacity", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        # Announced once on the way in, so a plugin that started while nothing could answer is
        # told rather than left scanning until the first change happens to occur.
        with self._lock:
            self._state = {"available": None, "ready": (), "waiting": {}, "checked_at": 0}
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                LOGGER.exception("decision capacity check failed")
            if self._stop.wait(self._interval):
                break

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            self._thread = None
