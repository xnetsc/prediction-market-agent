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
from prediction_market_agent.runtime.events import RobotEventLoop, PlatformDiscoveryEvent
from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop
from prediction_market_agent.core.config import ApplicationConfigStore
from prediction_market_agent.plugin_system.managed_config import save_managed_config


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

    def test_saved_pause_survives_a_refresh_while_the_old_runtime_is_still_busy(self) -> None:
        root = Path(tempfile.mkdtemp())
        application = root / "application.json"
        ApplicationConfigStore(application).save({"working_directory": str(root)})
        manager = RobotRuntimeManager(application)
        manager._status.update(running=True, robot_paused=False,
                               platforms={"polymarket": {"running": True, "paused": False}})
        manager._status_locked()
        save_managed_config(root / "bot_management.json", {
            "robot_paused": True, "paused_platforms": ["polymarket"],
        })
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with manager._lock:
                manager._busy_since = time.time()
                holding.set()
                release.wait(5)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(holding.wait(2))
        try:
            answer = manager.status()
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(answer["robot_paused"])
        self.assertTrue(answer["platforms"]["polymarket"]["paused"])
        self.assertTrue(answer["running"], "an in-flight call must not be reported as stopped")
        self.assertTrue(answer["settling"])
        self.assertIn("暂停已保存", answer["settling_reason"])


class PauseClosesTheBusinessEventGateTests(unittest.TestCase):
    def test_pause_blocks_the_next_decision_and_order_execution(self) -> None:
        from prediction_market_agent.runtime.engine import TradingEngine
        from prediction_market_agent.runtime.actions import ExecutionActionsMixin

        engine = TradingEngine.__new__(TradingEngine)
        engine.stop_requested = lambda: True
        self.assertFalse(engine.can_decide("polymarket"))

        class PausedAction(ExecutionActionsMixin):
            stop_requested = staticmethod(lambda: True)

            def _record_no_action(self, *args):
                return {"status": "NO_ACTION", "action": args[3]}

        action = PausedAction()
        decision = SimpleNamespace(to_dict=lambda: {"action": "BUY"})
        runtime = SimpleNamespace(plugin=SimpleNamespace(name="polymarket"),
                                  gateway=SimpleNamespace(place_order=lambda *a, **k: self.fail("order placed")))
        detail = SimpleNamespace(topic=SimpleNamespace(topic_id="topic"))
        market = SimpleNamespace(market_id="market")
        result = action._execute_decision(
            runtime, decision, detail=detail, market=market, token_id="token",
            bid=0.4, ask=0.6, decision_id=1,
        )
        self.assertEqual(result, {"status": "NO_ACTION", "action": "PAUSED"})

    def test_new_and_queued_events_do_not_run_after_pause(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        seen = []

        def handle(event):
            seen.append(event.platform)
            entered.set()
            release.wait(5)

        loop = RobotEventLoop(handle)
        loop.start()
        results = []

        def submit(name):
            try:
                loop.submit(PlatformDiscoveryEvent(platform=name))
            except RuntimeError as error:
                results.append(str(error))

        active = threading.Thread(target=submit, args=("active",), daemon=True)
        queued = threading.Thread(target=submit, args=("queued",), daemon=True)
        active.start()
        self.assertTrue(entered.wait(2))
        queued.start()
        deadline = time.time() + 2
        while loop.status()["queued"] != 1 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(loop.status()["queued"], 1)
        loop.request_pause()
        with self.assertRaises(RuntimeError):
            loop.submit(PlatformDiscoveryEvent(platform="late"))
        release.set()
        active.join(2)
        queued.join(2)
        loop.stop()
        self.assertEqual(seen, ["active"])
        self.assertEqual(len(results), 1)

    def test_discovery_finishing_after_stop_cannot_enter_decision(self) -> None:
        loop = PolymarketEventLoop(lambda: SimpleNamespace(
            scan_interval_seconds=60, error_backoff_seconds=30, error_backoff_max_seconds=900,
        ))
        entered = threading.Event()
        release = threading.Event()
        decisions = []

        def discover():
            entered.set()
            release.wait(5)
            return []

        loop.start({"discover_markets": discover,
                    "submit_scan": lambda topics: decisions.append(topics)})
        self.assertTrue(entered.wait(2))
        stopping = threading.Thread(target=loop.stop, daemon=True)
        stopping.start()
        self.assertTrue(loop._stop.wait(2))
        release.set()
        stopping.join(6)
        self.assertEqual(decisions, [])


class StoppingDoesNotWaitOutAModelCallTests(unittest.TestCase):
    """The answer is already being paid for; nothing is gained by holding the console hostage."""

    def _loop(self) -> PolymarketEventLoop:
        return PolymarketEventLoop(lambda: SimpleNamespace(
            scan_interval_seconds=60, error_backoff_seconds=30, error_backoff_max_seconds=900,
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
