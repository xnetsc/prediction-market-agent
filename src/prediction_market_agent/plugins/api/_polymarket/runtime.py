from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .config import PolymarketPluginConfig


class PolymarketEventLoop:
    """Polymarket-owned scan schedule and failure backoff.

    This plugin owns when a cycle runs. Which markets that cycle looks at, and which read
    endpoints get called to find them, belong to the framework's discovery callback.
    """

    def __init__(self, load_settings: Callable[[], PolymarketPluginConfig]):
        self._load_settings = load_settings
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # A worker that was told to stop and is still finishing the model call it was in.
        self._settling: threading.Thread | None = None
        self._status: dict[str, object] = {
            "running": False,
            "cycles": 0,
            "failures": 0,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": "",
            "next_delay_seconds": None,
            "current_stage": "idle",
            "holding": False,
            "holding_because": "",
        }
        # Scanning exists to feed a decision. While nothing can make one, a cycle would spend this
        # venue's rate budget to build a prompt that gets discarded at the last step, so this
        # plugin stands down and waits to be told otherwise. The framework only states the fact;
        # standing down is this plugin's own call.
        self._may_decide = threading.Event()
        self._may_decide.set()

    def start(self, services: dict[str, object]) -> None:
        callback = services.get("submit_scan")
        if not callable(callback):
            raise ValueError("Polymarket runtime requires a callable submit_scan service")
        # Optional: a plugin that never asks keeps its fixed interval and nothing changes.
        pacing = services.get("next_scan_delay")
        discover = services.get("discover_markets")
        if not callable(discover):
            raise ValueError("Polymarket runtime requires a callable discover_markets service")
        settings = self._load_settings()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # The previous worker was told to stop and has not got there yet - it is inside a model
            # call that cannot be interrupted. Starting now would put two cycles on one account,
            # both trading. The caller retries, and by then the old one has finished.
            if self._settling is not None and self._settling.is_alive():
                self._status["settling"] = True
                return
            self._settling = None
            self._status["settling"] = False
            self._status.pop("settling_reason", None)
            self._stop.clear()
            self._status.update({"running": True, "last_error": ""})
            self._thread = threading.Thread(
                target=self._run,
                args=(callback, discover, settings, pacing),
                name="prediction-polymarket-runtime",
                daemon=True,
            )
            self._thread.start()

    def _run(
        self,
        callback: Callable[..., object],
        discover: Callable[..., Any],
        settings: PolymarketPluginConfig,
        pacing: Callable[..., dict] | None = None,
    ) -> None:
        consecutive_failures = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    self._status["last_started_at"] = int(time.time())
                    self._status["current_stage"] = "discovery"
                try:
                    topics = discover()
                    with self._lock:
                        self._status["current_stage"] = "decision"
                    callback(topics)
                    consecutive_failures = 0
                    # The configured interval is how often this plugin is willing to be asked, not
                    # how often there is anything worth looking at. How fast this venue actually
                    # moves is a judgement about it right now, and the agent has just read it - so
                    # it may ask to wait longer. It cannot ask to come back sooner: that bound is
                    # this plugin's to keep.
                    delay = settings.scan_interval_seconds
                    if callable(pacing):
                        with self._lock:
                            self._status["current_stage"] = "pacing"
                        try:
                            answer = pacing(settings.scan_interval_seconds)
                            delay = max(
                                settings.scan_interval_seconds, int(answer.get("seconds", delay))
                            )
                        except Exception:
                            delay = settings.scan_interval_seconds
                    with self._lock:
                        self._status["cycles"] = int(self._status["cycles"]) + 1
                        self._status["last_error"] = ""
                except Exception as error:
                    consecutive_failures += 1
                    delay = min(
                        settings.error_backoff_max_seconds,
                        settings.error_backoff_seconds * (2 ** (consecutive_failures - 1)),
                    )
                    with self._lock:
                        self._status["failures"] = int(self._status["failures"]) + 1
                        self._status["last_error"] = str(error)
                with self._lock:
                    self._status["last_finished_at"] = int(time.time())
                    self._status["next_delay_seconds"] = delay
                    self._status["current_stage"] = "waiting"
                if self._stop.wait(delay):
                    break
                self._wait_until_decisions_are_possible()
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["next_delay_seconds"] = None
                self._status["current_stage"] = "idle"

    def _wait_until_decisions_are_possible(self) -> None:
        while not self._stop.is_set() and not self._may_decide.is_set():
            self._may_decide.wait(1.0)

    def notify(self, message: dict[str, object]) -> None:
        """Take the framework's word on whether a decision is reachable, and schedule accordingly."""
        if str(message.get("kind", "")) != "decision_capacity":
            return
        available = bool(message.get("available"))
        waiting = message.get("waiting") or {}
        with self._lock:
            self._status["holding"] = not available
            self._status["holding_because"] = (
                "" if available else "没有可用的 AI 模型服务：" + "；".join(
                    f"{name}（{reason}）" for name, reason in dict(waiting).items()
                )
            )
        if available:
            self._may_decide.set()
        else:
            self._may_decide.clear()

    STOP_WAIT_SECONDS = 5.0
    """How long stopping waits for the worker before answering. See the note inside stop()."""

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        self._may_decide.set()
        if thread is not None and thread is not threading.current_thread():
            # Bounded on purpose. A cycle that is inside a model call cannot be interrupted - the
            # answer is already being paid for - and waiting for it here means holding whatever
            # lock the caller took, which is how one slow decision froze every page of the console.
            # The thread is a daemon, it stops at its next check, and the status below says so.
            thread.join(timeout=self.STOP_WAIT_SECONDS)
        with self._lock:
            still_running = thread is not None and thread.is_alive()
            # Kept, not dropped: whoever starts next has to be able to see that it is still there.
            self._settling = thread if still_running else None
            self._thread = None
            self._status["running"] = False
            self._status["next_delay_seconds"] = None
            self._status["settling"] = still_running
            if still_running:
                self._status["settling_reason"] = (
                    "已经要求停止，但上一轮还在一次模型调用里，要等它自己结束才会真正退出；"
                    "这期间不会开始新的一轮。"
                )
            else:
                self._status.pop("settling_reason", None)

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)
