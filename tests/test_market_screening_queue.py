from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from prediction_market_agent.agent.decision_evaluator import CandidateAssessment
from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin
from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop
from prediction_market_agent.runtime.memory import SessionMemory
from prediction_market_agent.runtime.market_discovery import DiscoveryEngine, _screening_breather
from prediction_market_agent.plugin_system.managed_config import (
    ManagedRuntimeConfig, save_managed_config,
)


class MarketScreeningQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.sqlite3"
        self.memory = SessionMemory(self.path)
        self.addCleanup(self.memory.close)

    def test_screening_breather_tracks_observed_call_not_fixed_benchmark(self) -> None:
        self.assertEqual(_screening_breather(0.01), 0.005)
        self.assertAlmostEqual(_screening_breather(0.22), 0.022)
        self.assertEqual(_screening_breather(8.0), 0.5)

    @staticmethod
    def candidate(number: int, price: str = "0.40") -> dict:
        return {
            "candidate_id": f"event:{number}", "topic_id": "event",
            "market_id": str(number), "title": f"Market {number}",
            "status": "OPEN", "material_key": price,
            "outcomes": [{"token_id": f"yes-{number}", "displayed_probability": float(price)}],
        }

    def test_all_contracts_remain_pending_across_restart_until_screened(self) -> None:
        candidates = [self.candidate(index) for index in range(100)]
        self.memory.queue_market_screening(
            platform="polymarket", candidates=candidates, topic_ids=["event"]
        )
        first = self.memory.due_market_screening(platform="polymarket", limit=3)
        self.assertEqual([item["candidate_id"] for item in first],
                         ["event:0", "event:1", "event:2"])
        for item in first:
            self.memory.complete_market_screening(
                platform="polymarket", candidate_id=item["candidate_id"],
                assessment=asdict(CandidateAssessment(item["candidate_id"], "DEFER", 0.2)),
            )
        self.assertEqual(self.memory.market_screening_counts(platform="polymarket"),
                         {"known": 100, "screened": 3, "due": 97, "failed": 0})
        reopened = SessionMemory(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.market_screening_counts(platform="polymarket")["due"], 97)
        self.assertEqual(reopened.due_market_screening(platform="polymarket", limit=1)[0]
                         ["candidate_id"], "event:3")

    def test_explicit_skip_expires_or_material_change_wakes_market(self) -> None:
        item = self.candidate(1)
        self.memory.queue_market_screening(
            platform="polymarket", candidates=[item], topic_ids=["event"]
        )
        self.assertTrue(self.memory.skip_market_screening(
            platform="polymarket", candidate_id="event:1", for_seconds=60,
            reason="Review after the announcement",
        ))
        self.assertEqual(self.memory.due_market_screening(platform="polymarket", limit=1), [])
        self.memory.queue_market_screening(
            platform="polymarket", candidates=[self.candidate(1, "0.44")],
            topic_ids=["event"],
        )
        self.assertEqual(len(self.memory.due_market_screening(platform="polymarket", limit=1)), 1)
        self.assertFalse(self.memory.skip_market_screening(
            platform="polymarket", candidate_id="unknown", for_seconds=60, reason="x"
        ))

    def test_completed_decision_creates_independent_durable_appointment(self) -> None:
        decision_id = self.memory.begin_decision(
            platform="polymarket", market_topic_id="event", market_id="market",
            token_id="yes", strategy_name="built_in", strategy_sha256="test", context={},
        )
        self.memory.complete_decision(
            decision_id, status="NO_ACTION",
            final_decision={"action": "HOLD", "revisit_when": "After the report",
                            "revisit_after_seconds": 60},
        )
        due = self.memory.next_market_review_at(platform="polymarket")
        assert due is not None
        self.assertGreater(due, int(time.time() * 1000))
        self.assertEqual(self.memory.due_market_reviews(
            platform="polymarket", now_ms=due - 1), [])
        [task] = self.memory.due_market_reviews(platform="polymarket", now_ms=due)
        self.assertEqual((task["topic_id"], task["market_id"], task["token_id"]),
                         ("event", "market", "yes"))
        self.memory.finish_market_review(task["id"], error="temporarily unavailable")
        self.assertIsNotNone(self.memory.next_market_review_at(platform="polymarket"))

    def test_gamma_listing_expands_an_event_to_each_open_market(self) -> None:
        plugin = PolymarketApiPlugin.__new__(PolymarketApiPlugin)
        raw = {
            "id": "event", "title": "Who wins?", "active": True,
            "markets": [
                {"id": "1", "conditionId": "one", "question": "Candidate one?",
                 "active": True, "acceptingOrders": True,
                 "outcomes": '["Yes","No"]', "outcomePrices": '["0.4","0.6"]',
                 "clobTokenIds": '["y1","n1"]'},
                {"id": "2", "conditionId": "two", "question": "Candidate two?",
                 "active": True, "acceptingOrders": True,
                 "outcomes": '["Yes","No"]', "outcomePrices": '["0.2","0.8"]',
                 "clobTokenIds": '["y2","n2"]'},
                {"id": "3", "conditionId": "closed", "question": "Already closed?",
                 "active": False, "acceptingOrders": False,
                 "outcomes": '["Yes","No"]', "clobTokenIds": '["y3","n3"]'},
            ],
        }
        plugin._event_by_id = {"event": raw}
        items = plugin.screening_candidates(plugin._topic(raw))
        self.assertEqual([item["candidate_id"] for item in items],
                         ["event:one", "event:two"])
        self.assertEqual(items[1]["outcomes"][0]["token_id"], "y2")

    def test_pausing_screening_preserves_queue_and_resume_consumes_it(self) -> None:
        self.memory.queue_market_screening(
            platform="polymarket", candidates=[self.candidate(1)], topic_ids=["event"]
        )

        class Evaluator:
            available = True
            per_candidate_requests = False
            errors: dict = {}
            fallback_errors: dict = {}

            def evaluate_candidates(self, _state, candidates):
                return [CandidateAssessment(item["candidate_id"], "DEFER", 0.2)
                        for item in candidates]

        paused = True
        discovery = DiscoveryEngine(
            memory=self.memory, strategy=None, provider=None, evaluator=Evaluator(),
            evolution_enabled=False, screening_enabled=lambda: not paused,
        )
        plugin = type("Plugin", (), {"name": "polymarket"})()
        self.assertEqual(discovery.screen_pending(plugin), {"screened": 0, "attempted": 0})
        self.assertEqual(self.memory.market_screening_counts(platform="polymarket")["due"], 1)
        paused = False
        self.assertEqual(discovery.screen_pending(plugin), {"screened": 1, "attempted": 1})
        self.assertEqual(self.memory.market_screening_counts(platform="polymarket")["due"], 0)

    def test_screening_pause_is_durable_and_does_not_change_robot_pause(self) -> None:
        config_path = Path(self.temp.name) / "selection.json"
        saved = save_managed_config(config_path, {
            "enabled": {"api": ["polymarket"]},
            "robot_paused": False, "screening_paused": True,
        })
        self.assertTrue(saved.screening_paused)
        self.assertFalse(ManagedRuntimeConfig.load(config_path).robot_paused)
        with self.assertRaisesRegex(ValueError, "screening_paused must be a boolean"):
            save_managed_config(config_path, {**saved.to_dict(), "screening_paused": "yes"})

    def test_discovery_has_one_screening_consumer_even_when_called_concurrently(self) -> None:
        self.memory.queue_market_screening(
            platform="polymarket", candidates=[self.candidate(1)], topic_ids=["event"]
        )
        entered = threading.Event()
        release = threading.Event()

        class SlowEvaluator:
            available = True
            per_candidate_requests = False
            errors: dict = {}
            fallback_errors: dict = {}

            def evaluate_candidates(self, _state, candidates):
                entered.set()
                release.wait(1)
                return [CandidateAssessment(item["candidate_id"], "DEFER", 0.2)
                        for item in candidates]

        discovery = DiscoveryEngine(
            memory=self.memory, strategy=None, provider=None,
            evaluator=SlowEvaluator(), evolution_enabled=False,
        )
        plugin = type("Plugin", (), {"name": "polymarket"})()
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(discovery.screen_pending, plugin)
            self.assertTrue(entered.wait(1))
            self.assertEqual(discovery.screen_pending(plugin),
                             {"screened": 0, "attempted": 0})
            release.set()
            self.assertEqual(first.result(timeout=1), {"screened": 1, "attempted": 1})


class AppointmentPriorityTests(unittest.TestCase):
    def test_due_appointment_precedes_background_screening(self) -> None:
        loop = PolymarketEventLoop(lambda: None)
        called: list[str] = []

        def review() -> dict:
            called.append("review")
            loop._stop.set()
            return {"reviewed": 1}

        def screen() -> dict:
            called.append("screen")
            return {"attempted": 1}

        loop._wait_for_scan_or_review(
            time.time() + 60, lambda: int(time.time()) - 1, review,
            lambda: int(time.time()) - 1, screen,
        )
        self.assertEqual(called, ["review"])


if __name__ == "__main__":
    unittest.main()
