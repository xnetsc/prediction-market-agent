from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.plugin_system.scan_schedule import (
    MAX_WAIT_SLICE_SECONDS,
    wait_until_wall_deadline,
)


class FakeStop:
    def __init__(self, now: list[float], *, stop_on_wait: bool = False):
        self.now = now
        self.stop_on_wait = stop_on_wait
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self.stop_on_wait:
            return True
        self.now[0] += seconds
        return False


class ScanScheduleTests(unittest.TestCase):
    def test_wait_is_bounded_and_uses_wall_time_after_host_sleep(self) -> None:
        now = [100.0]
        stop = FakeStop(now)

        def slept_wait(seconds: float) -> bool:
            stop.waits.append(seconds)
            now[0] += 3600.0  # host slept while monotonic wait barely advanced
            return False

        stop.wait = slept_wait
        with patch("prediction_market_agent.plugin_system.scan_schedule.time.time", side_effect=lambda: now[0]):
            self.assertFalse(wait_until_wall_deadline(stop, 3700.0))
        self.assertEqual(stop.waits, [MAX_WAIT_SLICE_SECONDS])

    def test_stop_interrupts_the_wait(self) -> None:
        now = [100.0]
        stop = FakeStop(now, stop_on_wait=True)
        with patch("prediction_market_agent.plugin_system.scan_schedule.time.time", side_effect=lambda: now[0]):
            self.assertTrue(wait_until_wall_deadline(stop, 3700.0))
        self.assertEqual(stop.waits, [MAX_WAIT_SLICE_SECONDS])

    def test_both_platforms_use_the_wall_deadline_and_expose_it(self) -> None:
        for platform in ("polymarket", "binance"):
            with self.subTest(platform=platform):
                source = Path(
                    f"src/prediction_market_agent/plugins/api/_{platform}/runtime.py"
                ).read_text()
                if platform == "polymarket":
                    self.assertIn("self._wait_for_scan_or_review(", source)
                    self.assertIn("self._stop.wait(remaining)", source)
                else:
                    self.assertIn("wait_until_wall_deadline(self._stop, deadline)", source)
                self.assertIn('self._status["next_run_at"] = int(deadline)', source)
                self.assertNotIn("self._stop.wait(delay)", source)
