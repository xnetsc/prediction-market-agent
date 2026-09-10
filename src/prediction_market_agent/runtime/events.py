from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..plugin_system.contracts import Topic


@dataclass(frozen=True)
class PlatformScanEvent:
    platform: str
    topics: tuple[Topic, ...]
    maximum_decisions: int
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class _Envelope:
    event: PlatformScanEvent
    completed: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class RobotEventLoop:
    """Generic business-event consumer; platform plugins own event production timing."""

    def __init__(self, handler: Callable[[PlatformScanEvent], Any]):
        self._handler = handler
        self._queue: queue.Queue[_Envelope | None] = queue.Queue()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._processed = 0
        self._failures = 0
        self._last_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="prediction-business-events",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            envelope = self._queue.get()
            try:
                if envelope is None:
                    return
                try:
                    envelope.result = self._handler(envelope.event)
                    with self._lock:
                        self._processed += 1
                        self._last_error = ""
                except BaseException as error:
                    envelope.error = error
                    with self._lock:
                        self._failures += 1
                        self._last_error = str(error)
                finally:
                    envelope.completed.set()
            finally:
                self._queue.task_done()

    def submit(self, event: PlatformScanEvent) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                raise RuntimeError("Robot event loop is not running")
        envelope = _Envelope(event)
        self._queue.put(envelope)
        envelope.completed.wait()
        if envelope.error is not None:
            raise envelope.error
        return envelope.result

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._queue.put(None)
        if thread is not threading.current_thread():
            thread.join()
        with self._lock:
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "queued": self._queue.qsize(),
                "processed": self._processed,
                "failures": self._failures,
                "last_error": self._last_error,
            }
