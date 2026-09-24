"""Wait for a scan's wall-clock deadline without extending it across host sleep."""

from __future__ import annotations

import threading
import time


MAX_WAIT_SLICE_SECONDS = 10.0


def wait_until_wall_deadline(stop: threading.Event, deadline: float) -> bool:
    """Return true if stopped; otherwise resume promptly after the deadline.

    A single Event.wait(hours) uses a monotonic timeout that may not advance while
    the host sleeps. Short waits let us recheck wall time after a wake instead of
    accidentally adding the sleep duration to the planned scan interval.
    """
    while not stop.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        if stop.wait(min(remaining, MAX_WAIT_SLICE_SECONDS)):
            return True
    return True
