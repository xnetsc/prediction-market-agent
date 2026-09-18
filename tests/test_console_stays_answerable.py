"""The console has to answer while the robot is changing, which is when it is most asked.

Saving a setting restarts the robot, and stopping it means waiting for a cycle that may be inside a
model call minutes long. That wait used to be taken while holding the lock the status endpoint
needs, with a console that polls on a timer whether or not the last answer came back: the requests
queued, the queue took every worker thread the server had, and then nothing answered at all - not
the save, not the ledger, not the health check. The failure looked like a hung page, so it was
reported as a saving bug rather than as a server that had stopped serving.
"""
from ._support import *
import threading
import time

from prediction_market_agent.runtime.controller import RobotRuntimeManager
from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop


class ReadingTheStateNeverQueuesTests(unittest.TestCase):
    def _manager(self) -> RobotRuntimeManager:
        return RobotRuntimeManager(Path(tempfile.mkdtemp()) / "application.json")

    def test_it_answers_from_the_last_known_state_while_something_long_holds_the_lock(self) -> None:
        manager = self._manager()
        manager._status.update(running=True, global_ready=True)
        manager._status_locked()  # the fresh answer that becomes the snapshot
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with manager._lock:
                manager._busy_since = time.time()
                holding.set()
                release.wait(10)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(holding.wait(5))
        started = time.time()
        answer = manager.status()
        waited = time.time() - started
        release.set()
        worker.join(5)
        self.assertLess(waited, 3.0, "status must not wait out a restart")
        self.assertTrue(answer["settling"])
        self.assertIn("重启", answer["settling_reason"])
        self.assertTrue(answer["running"], "the last known state is still reported")

    def test_an_ordinary_read_takes_the_fresh_path(self) -> None:
        manager = self._manager()
        answer = manager.status()
        self.assertNotIn("settling", answer)


class StoppingDoesNotWaitOutAModelCallTests(unittest.TestCase):
    """The answer is already being paid for; nothing is gained by holding the console hostage."""

    def _loop(self) -> PolymarketEventLoop:
        return PolymarketEventLoop(lambda: SimpleNamespace(
            scan_interval_seconds=60, error_backoff_seconds=30, error_backoff_max_seconds=900,
            max_topics_per_cycle=10, max_decisions_per_cycle=6,
        ))

    def test_stop_returns_while_the_worker_is_still_finishing(self) -> None:
        loop = self._loop()
        busy = threading.Event()
        finish = threading.Event()

        def cycle(*_args, **_kwargs):
            busy.set()
            finish.wait(30)
            return []

        loop.start({"submit_scan": lambda *a, **k: None, "discover_markets": cycle})
        self.assertTrue(busy.wait(5))
        started = time.time()
        loop.stop()
        waited = time.time() - started
        self.assertLess(waited, PolymarketEventLoop.STOP_WAIT_SECONDS + 3)
        status = loop.status()
        self.assertFalse(status["running"])
        self.assertTrue(status["settling"], "it has not actually stopped yet, and says so")
        self.assertIn("模型调用", status["settling_reason"])

        # And nothing starts a second cycle on the same account while the first is still there.
        loop.start({"submit_scan": lambda *a, **k: None, "discover_markets": cycle})
        self.assertFalse(loop.status()["running"])
        finish.set()

    def test_once_it_has_finished_a_start_is_allowed_again(self) -> None:
        loop = self._loop()
        loop.start({"submit_scan": lambda *a, **k: None, "discover_markets": lambda *a, **k: []})
        loop.stop()
        for _ in range(50):
            if not loop.status().get("settling"):
                break
            time.sleep(0.1)
        loop.start({"submit_scan": lambda *a, **k: None, "discover_markets": lambda *a, **k: []})
        self.assertTrue(loop.status()["running"])
        loop.stop()


class ALargeDeleteHandsTheSpaceBackTests(unittest.TestCase):
    """A delete that touches every page copies every page into the log, and it stays there."""

    def test_the_log_is_capped_and_drained(self) -> None:
        from prediction_market_agent.runtime.memory import SessionMemory

        memory = SessionMemory(Path(tempfile.mkdtemp()) / "session.sqlite3")
        self.addCleanup(memory.connection.close)
        limit = memory.connection.execute("PRAGMA journal_size_limit").fetchone()[0]
        self.assertEqual(int(limit), SessionMemory.WAL_LIMIT_BYTES)
        for index in range(50):
            memory.begin_decision(
                platform="p", market_topic_id=f"t{index}", market_id="m", token_id=f"o{index}",
                strategy_name="s", strategy_sha256="x", context={"filler": "x" * 2000},
            )
        memory.forget_decisions(decision_ids=None, status="", platform="p")
        self.assertTrue(memory.reclaim_log()["reclaimed"])
        wal = memory.path.with_name(memory.path.name + "-wal")
        self.assertLessEqual(wal.stat().st_size if wal.exists() else 0, SessionMemory.WAL_LIMIT_BYTES)

    def test_deleting_a_whole_category_drains_it_without_being_asked(self) -> None:
        source = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        self.assertIn('"log": self._reclaim_log()', source)
        self.assertIn("def _reclaim_log", source)


if __name__ == "__main__":
    unittest.main()
