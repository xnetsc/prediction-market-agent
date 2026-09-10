from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .config import BinancePluginConfig


class BinanceEventLoop:
    """Binance-owned scan schedule and failure backoff."""

    def __init__(
        self,
        load_settings: Callable[[], BinancePluginConfig],
        scan: Callable[[int, int], tuple[Any, ...]],
    ):
        self._load_settings = load_settings
        self._scan = scan
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, object] = {
            "running": False,
            "cycles": 0,
            "failures": 0,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": "",
            "next_delay_seconds": None,
        }

    def start(self, services: dict[str, object]) -> None:
        callback = services.get("submit_scan")
        if not callable(callback):
            raise ValueError("Binance runtime requires a callable submit_scan service")
        settings = self._load_settings()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status.update({"running": True, "last_error": ""})
            self._thread = threading.Thread(
                target=self._run,
                args=(callback, settings),
                name="prediction-binance-runtime",
                daemon=True,
            )
            self._thread.start()

    def _run(self, callback: Callable[..., object], settings: BinancePluginConfig) -> None:
        consecutive_failures = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    self._status["last_started_at"] = int(time.time())
                try:
                    topics = self._scan(
                        settings.max_topics_per_cycle,
                        settings.topic_page_size,
                    )
                    callback(topics, settings.max_decisions_per_cycle)
                    consecutive_failures = 0
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
                if self._stop.wait(delay):
                    break
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["next_delay_seconds"] = None

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._lock:
            self._thread = None
            self._status["running"] = False
            self._status["next_delay_seconds"] = None

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)
