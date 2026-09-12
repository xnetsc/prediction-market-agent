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

LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
"""How often the fact is re-read. It is a local lookup, so this is about how quickly work resumes."""


class DecisionCapacityWatch:
    """Reports whether any decision provider is currently able to answer, and when that changes."""

    def __init__(
        self,
        provider: Any,
        listeners: Callable[[], list[tuple[str, Callable[[dict[str, Any]], None]]]],
        *,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._provider = provider
        self._listeners = listeners
        self._interval = max(5, int(interval_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"available": True, "ready": (), "waiting": {}, "checked_at": 0}

    def reading(self) -> dict[str, Any]:
        """Who can answer right now, and for those who cannot, what they are waiting out.

        Backoff is what makes this answerable without asking anything: a provider that failed had
        its failure classified and a retry time set, so being unavailable is already recorded and
        does not have to be rediscovered by failing again.
        """
        names = tuple(getattr(self._provider, "available_names", ()) or ())
        health = getattr(self._provider, "health", None)
        ready: list[str] = []
        waiting: dict[str, str] = {}
        now = time.time()
        for name in names:
            state = health.state(name) if health is not None else None
            if state is None or state.available_at(now):
                ready.append(name)
            else:
                waiting[name] = str(getattr(state, "last_error_kind", "") or "unavailable")
        unavailable = dict(getattr(self._provider, "unavailable", {}) or {})
        for name, reason in unavailable.items():
            waiting.setdefault(name, str(reason)[:200])
        return {
            "available": bool(ready),
            "ready": tuple(ready),
            "waiting": waiting,
            "checked_at": int(now),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def check(self) -> dict[str, Any]:
        """Read it once and tell the listeners if the answer turned over."""
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
