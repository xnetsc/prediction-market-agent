from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from prediction_market_agent.agent.decision import AgentRunResult
from prediction_market_agent.agent.evolution import (
    DECISION_EVOLUTION_KEY,
    DISCOVERY_EVOLUTION_KEY,
    LESSON_MIN_SAMPLE,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    shrunk_weight,
)
from prediction_market_agent.agent.market_discovery import (
    BuiltInMarketDiscovery,
    compose_discovery_payload,
)
from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy
from prediction_market_agent.plugin_system.contracts import (
    ApiCapabilities,
    Market,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
)
from prediction_market_agent.runtime.decision_strategy import DecisionEvolution
from prediction_market_agent.runtime.market_discovery import DiscoveryEngine
from prediction_market_agent.runtime.memory import SessionMemory


CAPABILITIES = ApiCapabilities(
    realtime_order_book=True,
    candles=False,
    market_search=False,
    settlement_status=True,
    supported_order_types=("MARKET",),
    write_workflows=("BUY",),
    data_features=("topic_list",),
)


class FakePlugin:
    name = "fake"
    capabilities = CAPABILITIES

    def __init__(self, topics: int = 60):
        self.topics = [
            Topic(str(i), f"Topic {i}", "q", "d", "crypto", "OPEN", 5_000.0, 1_000.0 * i, f"t{i}")
            for i in range(1, topics + 1)
        ]
        self.list_calls = 0
        self.detail_calls = 0
        self.book_calls = 0

    def topic_page_size(self) -> int:
        return 25

    def cycle_limits(self) -> tuple[int, int]:
        return (5, 5)

    def list_topics(self, *, offset: int, limit: int) -> TopicPage:
        self.list_calls += 1
        page = self.topics[offset : offset + limit]
        return TopicPage(tuple(page), offset + limit < len(self.topics), offset + len(page))

    def get_topic(self, topic_id: str) -> TopicDetail:
        self.detail_calls += 1
        topic = next(item for item in self.topics if item.topic_id == topic_id)
        outcome = Outcome(f"yes-{topic_id}", "YES", 0.4)
        return TopicDetail(
            topic=topic,
            start_time_ms=0,
            end_time_ms=int(time.time() * 1000) + 86_400_000,
            fee_bps=10,
            markets=(Market(f"m{topic_id}", "T", "Q", "OPEN", 100.0, 100.0, (outcome,)),),
        )

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook:
        self.book_calls += 1
        return OrderBook((PriceLevel(0.39, 10),), (PriceLevel(0.41, 10),), 0)


class RecordingProvider:
    """Selects the shortlist head and records what the framework asked it."""

    name = "recording"

    def __init__(self, tools: tuple[str, ...] = ()):
        self.requests: list[dict] = []
        self.instructions = ""
        self.tool_results: list[dict] = []
        self.tools = tools

    def run(self, payload, **options):
        self.requests.append(payload)
        self.instructions = options.get("instructions", "")
        executor = options.get("tool_executor")
        for name in self.tools:
            first = payload["candidates"][0]
            arguments = (
                {"topic_id": first["topic_id"]}
                if name != "OUTCOME_BOOK"
                else {"market_id": f"m{first['topic_id']}", "outcome_id": "yes"}
            )
            self.tool_results.append(executor(name, arguments))
        picks = payload["candidates"][: payload["maximum_selections"]]
        return AgentRunResult(
            value={
                "selections": [
                    {"topic_id": item["topic_id"], "reason": "measured change", "priors": ["fresh_listing"]}
                    for item in picks
                ],
                "skipped_reason": "",
            },
            raw_output="{}",
            provider=self.name,
            research_trace=[],
        )


class LegacyProvider:
    """A provider that predates the discovery contract and has no run()."""

    name = "legacy"

    def decide(self, payload, **options):  # pragma: no cover - never reached
        raise AssertionError("discovery must not call decide()")


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def _engine(self, provider, *, evolution: bool = True) -> DiscoveryEngine:
        return DiscoveryEngine(
            memory=self.memory,
            strategy=BuiltInMarketDiscovery(),
            provider=provider,
            evolution_enabled=evolution,
        )

    def test_survey_is_wider_than_the_platform_submission_ceiling(self) -> None:
        plugin, provider = FakePlugin(60), RecordingProvider()
        selected = self._engine(provider).discover(
            platform="fake", plugin=plugin, maximum_topics=5
        )
        observed = self.memory.connection.execute(
            "SELECT COUNT(*) FROM topic_observations"
        ).fetchone()[0]
        self.assertEqual(len(selected), 5)
        self.assertEqual(observed, 60, "every surveyed topic must be snapshotted")
        self.assertGreater(
            len(provider.requests[0]["candidates"]),
            len(selected),
            "the Agent must choose from a shortlist wider than the final slate",
        )

    def test_selection_rotates_instead_of_repeating_the_same_slate(self) -> None:
        plugin, provider = FakePlugin(60), RecordingProvider()
        engine = self._engine(provider)
        first = engine.discover(platform="fake", plugin=plugin, maximum_topics=5)
        second = engine.discover(platform="fake", plugin=plugin, maximum_topics=5)
        self.assertTrue(first and second)
        self.assertFalse(
            {topic.topic_id for topic in first} & {topic.topic_id for topic in second},
            "topics just analyzed must not immediately win the next slate",
        )

    def test_agent_drives_reads_through_plugin_interfaces_within_budget(self) -> None:
        plugin = FakePlugin(30)
        provider = RecordingProvider(tools=("TOPIC_DETAIL", "OUTCOME_BOOK", "TOPIC_HISTORY"))
        self._engine(provider).discover(platform="fake", plugin=plugin, maximum_topics=3)
        self.assertEqual(plugin.detail_calls, 1)
        self.assertEqual(plugin.book_calls, 1)
        self.assertTrue(all(result.get("ok") for result in provider.tool_results))
        self.assertIn("MARKET_DISCOVERY_STRATEGY", provider.instructions)
        self.assertIn("MISSION", provider.instructions)

    def test_missing_agent_degrades_to_prescore_and_is_recorded_as_such(self) -> None:
        plugin = FakePlugin(20)
        selected = self._engine(LegacyProvider()).discover(
            platform="fake", plugin=plugin, maximum_topics=4
        )
        strategies = {
            row[0]
            for row in self.memory.connection.execute(
                "SELECT DISTINCT strategy FROM discovery_selections"
            )
        }
        self.assertEqual(len(selected), 4, "a failing Agent must not stop the platform")
        self.assertEqual(strategies, {"built_in:mechanical"})

    def test_budget_refuses_reads_beyond_the_configured_allowance(self) -> None:
        plugin = FakePlugin(30)
        budget = BuiltInMarketDiscovery().budget()
        provider = RecordingProvider(tools=("TOPIC_DETAIL",) * (budget.detail_lookups + 2))
        self._engine(provider).discover(platform="fake", plugin=plugin, maximum_topics=2)
        refused = [item for item in provider.tool_results if not item.get("ok")]
        self.assertEqual(plugin.detail_calls, budget.detail_lookups)
        self.assertTrue(refused and "budget exhausted" in refused[0]["error"])


class EvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def _record(self, count: int, buckets: list[str], priors: list[str], useful: bool) -> None:
        for index in range(count):
            row = self.memory.record_discovery_selection(
                platform="fake",
                strategy="built_in",
                market_topic_id=f"{'u' if useful else 'x'}{index}",
                position=1,
                reason="r",
                priors=priors,
                features={"buckets": buckets},
            )
            self.memory.mark_selection_reviewed(row, {"useful": useful, "acted": int(useful)})

    def _engine(self, *, evolution: bool = True) -> DiscoveryEngine:
        return DiscoveryEngine(
            memory=self.memory,
            strategy=BuiltInMarketDiscovery(),
            provider=None,
            evolution_enabled=evolution,
        )

    def test_small_samples_barely_move_a_weight_and_bounds_hold(self) -> None:
        self.assertLess(shrunk_weight(1.0, 0.3, 1), 1.2)
        self.assertLess(shrunk_weight(1.0, 0.3, 1), shrunk_weight(1.0, 0.3, 20))
        self.assertEqual(shrunk_weight(1.0, 0.3, 10_000), WEIGHT_CEILING)
        self.assertEqual(shrunk_weight(0.0, 0.3, 10_000), WEIGHT_FLOOR)
        self.assertEqual(shrunk_weight(1.0, 0.0, 50), 1.0)

    def test_a_bucket_under_the_sample_gate_produces_no_lesson(self) -> None:
        self._record(LESSON_MIN_SAMPLE - 1, ["flow:moving"], ["fresh_listing"], True)
        self._record(LESSON_MIN_SAMPLE - 1, ["flow:quiet"], ["fresh_listing"], False)
        self._engine().review()
        self.assertEqual(self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY), [])

    def test_lessons_state_the_measurement_and_retire_when_it_stops_holding(self) -> None:
        self._record(30, ["flow:moving"], ["stale_price_moving_reality"], True)
        self._record(30, ["flow:quiet"], ["fresh_listing"], False)
        self._engine().review()
        lessons = {
            item["bucket"]: item
            for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)
        }
        self.assertIn("flow:moving", lessons)
        self.assertIn("30 selections", lessons["flow:moving"]["text"])
        weights = {
            item["prior_id"]: item["weight"]
            for item in self.memory.load_discovery_priors(DISCOVERY_EVOLUTION_KEY)
        }
        self.assertGreater(weights["stale_price_moving_reality"], weights["fresh_listing"])

        # The quiet bucket now performs exactly like the rest, so its lesson no longer holds.
        self._record(120, ["flow:quiet"], ["fresh_listing"], True)
        self._engine().review()
        remaining = {
            item["bucket"]
            for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)
        }
        self.assertNotIn("flow:quiet", remaining)

    def test_switching_evolution_off_restores_the_operator_text_exactly(self) -> None:
        self._record(30, ["flow:moving"], ["fresh_listing"], True)
        self._record(30, ["flow:quiet"], ["fresh_listing"], False)
        self._engine().review()
        strategy = BuiltInMarketDiscovery()
        now = int(time.time() * 1000)
        on = self._engine(evolution=True)
        off = self._engine(evolution=False)
        payload_on = compose_discovery_payload(
            strategy,
            priors=on._priors(),
            lessons=on._lessons(),
            measurements=on.measurements(),
            evolution_enabled=True,
            now_ms=now,
        )
        payload_off = compose_discovery_payload(
            strategy,
            priors=off._priors(),
            lessons=off._lessons(),
            measurements={},
            evolution_enabled=False,
            now_ms=now,
        )
        self.assertTrue(payload_on["lessons"])
        self.assertNotIn("lessons", payload_off)
        self.assertNotIn("priors", payload_off)
        self.assertEqual(payload_off["instructions"], strategy.instructions)
        self.assertFalse(payload_off["evolution"]["enabled"])

    def test_measurement_keeps_running_while_the_overlay_is_switched_off(self) -> None:
        self._record(30, ["flow:moving"], ["fresh_listing"], True)
        self._record(30, ["flow:quiet"], ["fresh_listing"], False)
        self._engine(evolution=False).review()
        self.assertTrue(
            self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY),
            "the built-in strategy must keep learning even when it is not the one applied",
        )


class DecisionStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def _settled(self, count: int, estimate: float, won: bool) -> None:
        for index in range(count):
            token = f"tk-{estimate}-{won}-{index}"
            decision_id = self.memory.begin_decision(
                platform="fake",
                market_topic_id=f"topic-{index}",
                market_id="m",
                token_id=token,
                strategy_name="built_in",
                strategy_sha256="x",
                context={},
            )
            self.memory.complete_decision(
                decision_id,
                provider="p",
                proposed_decision={
                    "action": "BUY",
                    "estimated_probability": estimate,
                    "confidence": 0.6,
                    "priors": ["calibrated_confidence"],
                },
                status="OK",
            )
            self.memory.record_action(
                platform="fake",
                market_topic_id=f"topic-{index}",
                token_id=token,
                action="REDEEM",
                request={"winning": won, "outcome": "YES"},
                result={"ok": True},
            )

    def _evolution(self, *, enabled: bool = True) -> DecisionEvolution:
        return DecisionEvolution(
            memory=self.memory,
            strategy=BuiltInDecisionStrategy(),
            evolution_enabled=enabled,
        )

    def test_built_in_strategy_gates_closed_markets_and_binary_mirrors(self) -> None:
        strategy = BuiltInDecisionStrategy()
        yes = Outcome("y", "YES", 0.4)
        no = Outcome("n", "NO", 0.6)
        multi = (Outcome("a", "A", 0.3), Outcome("b", "B", 0.3), Outcome("c", "C", 0.4))
        self.assertEqual([item.name for item in strategy.select_outcomes((yes, no))], ["YES"])
        self.assertEqual(len(strategy.select_outcomes(multi)), 3)
        markets = (
            Market("open", "T", "Q", "OPEN", 1.0, 1.0, (yes,)),
            Market("shut", "T", "Q", "CLOSED", 1.0, 1.0, (yes,)),
        )
        self.assertEqual([item.market_id for item in strategy.select_markets(markets)], ["open"])

    def test_overconfidence_becomes_a_calibration_lesson(self) -> None:
        # 30 markets priced around 0.8 that only resolved YES a third of the time.
        self._settled(10, 0.8, True)
        self._settled(20, 0.8, False)
        result = self._evolution().review()
        lessons = self.memory.load_discovery_lessons(DECISION_EVOLUTION_KEY)
        self.assertEqual(result["settled"], 30)
        self.assertEqual(len(lessons), 1)
        self.assertIn("overconfident", lessons[0]["text"])
        self.assertIn("30 settled markets", lessons[0]["text"])

    def test_decision_overlay_reaches_the_agent_and_respects_the_switch(self) -> None:
        self._settled(10, 0.8, True)
        self._settled(20, 0.8, False)
        self._evolution().review()
        payload = self._evolution().payload()
        self.assertTrue(payload["lessons"])
        self.assertEqual(payload["measured_outcomes"]["settled"], 30)
        off = self._evolution(enabled=False).payload()
        self.assertNotIn("lessons", off)
        self.assertEqual(off["instructions"], BuiltInDecisionStrategy().instructions)

    def test_discovery_and_decision_overlays_do_not_mix(self) -> None:
        self._settled(10, 0.8, True)
        self._settled(20, 0.8, False)
        self._evolution().review()
        self.assertTrue(self.memory.load_discovery_lessons(DECISION_EVOLUTION_KEY))
        self.assertEqual(self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY), [])


class StrategyExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_export_reports_what_each_lane_sends_to_the_model(self) -> None:
        from prediction_market_agent.core.config import Config
        from prediction_market_agent.runtime.strategy_export import export_strategies

        config = Config(session_db=self.root / "session.sqlite3")
        result = export_strategies(config)
        self.assertEqual(set(result["lanes"]), {"discovery", "decision"})
        for lane, view in result["lanes"].items():
            with self.subTest(lane=lane):
                self.assertEqual(view["source"], "built-in")
                self.assertTrue(view["sha256"])
                self.assertIn(view["instructions"][:40], view["prompt_text"])
                self.assertGreater(view["prompt_chars"], 500)
                self.assertFalse(
                    view["evolution"]["switchable"],
                    "the built-in strategies evolve unconditionally",
                )

    def test_export_carries_measured_lessons_into_the_exported_text(self) -> None:
        from prediction_market_agent.core.config import Config
        from prediction_market_agent.runtime.strategy_export import export_strategies

        memory = SessionMemory(self.root / "session.sqlite3")
        self.addCleanup(memory.close)
        # A lesson needs contrast: a bucket only stands out against a different baseline.
        for bucket, useful in (("flow:moving", True), ("flow:quiet", False)):
            for index in range(30):
                row = memory.record_discovery_selection(
                    platform="fake",
                    strategy="built_in",
                    market_topic_id=f"{bucket}-{index}",
                    position=1,
                    reason="r",
                    priors=["fresh_listing"],
                    features={"buckets": [bucket]},
                )
                memory.mark_selection_reviewed(row, {"useful": useful, "acted": int(useful)})
        DiscoveryEngine(
            memory=memory,
            strategy=BuiltInMarketDiscovery(),
            provider=None,
            evolution_enabled=True,
        ).review()
        view = export_strategies(
            Config(session_db=self.root / "session.sqlite3"),
            lane="discovery",
            memory=memory,
        )["lanes"]["discovery"]
        self.assertTrue(view["lessons_all"])
        self.assertIn("LESSONS_MEASURED_BY_THIS_RUNTIME", view["prompt_text"])
        self.assertIn("flow:moving", view["prompt_text"])

    def test_overlay_stays_bounded_as_measured_buckets_multiply(self) -> None:
        from prediction_market_agent.agent.evolution import (
            OVERLAY_MAX_BUCKETS,
            OVERLAY_MAX_LESSONS,
            prompt_json_payload,
        )

        memory = SessionMemory(self.root / "session.sqlite3")
        self.addCleanup(memory.close)
        for group in range(14):
            for index in range(30):
                row = memory.record_discovery_selection(
                    platform="fake",
                    strategy="built_in",
                    market_topic_id=f"{group}-{index}",
                    position=1,
                    reason="r",
                    priors=["fresh_listing"],
                    features={"buckets": [f"category:c{group}", f"liquidity:l{group}"]},
                )
                useful = group % 2 == 0
                memory.mark_selection_reviewed(
                    row, {"useful": useful, "acted": int(useful)}
                )
        engine = DiscoveryEngine(
            memory=memory,
            strategy=BuiltInMarketDiscovery(),
            provider=None,
            evolution_enabled=True,
        )
        engine.review()
        payload = compose_discovery_payload(
            BuiltInMarketDiscovery(),
            priors=engine._priors(),
            lessons=engine._lessons(),
            measurements=engine.measurements(),
            evolution_enabled=True,
            now_ms=int(time.time() * 1000),
        )
        stored = len(memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY))
        self.assertGreater(stored, OVERLAY_MAX_LESSONS, "this fixture must overflow the cap")
        self.assertLessEqual(len(payload["lessons"]), OVERLAY_MAX_LESSONS)
        self.assertEqual(payload["lessons_withheld"], stored - len(payload["lessons"]))
        self.assertLessEqual(
            len(payload["measured_outcomes"]["buckets"]), OVERLAY_MAX_BUCKETS
        )
        self.assertGreater(payload["measured_outcomes"]["buckets_tracked"], OVERLAY_MAX_BUCKETS)
        stripped = prompt_json_payload(payload)
        self.assertNotIn("instructions", stripped)
        self.assertIn("lessons_applied", stripped)


class EvolutionSwitchTests(unittest.TestCase):
    """The switch is the operator's only control over evolution, so it must actually stick."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _service(self):
        from prediction_market_agent.core.config import Config
        from prediction_market_agent.plugin_system.management import PluginManagementService

        config = Config(
            working_directory=self.root,
            management_file=self.root / "bot_management.json",
            session_db=self.root / "session.sqlite3",
            state_file=self.root / "state.json",
        )
        return PluginManagementService(config)

    def test_turning_evolution_off_survives_the_refresh_that_follows_the_save(self) -> None:
        service = self._service()
        self.assertTrue(service.manifest()["strategy_evolution"], "default is on")
        service.save_enabled(
            {"enabled": {"api": []}, "decision_strategy": "", "strategy_evolution": False}
        )
        self.assertFalse(
            service.manifest()["strategy_evolution"],
            "saving the switch off must persist",
        )
        service.refresh()
        self.assertFalse(
            service.manifest()["strategy_evolution"],
            "refresh re-saves the managed file and must not reset the switch",
        )
        service.save_enabled(
            {"enabled": {"api": []}, "decision_strategy": "", "strategy_evolution": True}
        )
        self.assertTrue(service.manifest()["strategy_evolution"])

    def test_an_omitted_switch_keeps_its_stored_value(self) -> None:
        service = self._service()
        service.save_enabled(
            {"enabled": {"api": []}, "decision_strategy": "", "strategy_evolution": False}
        )
        service.save_enabled({"enabled": {"api": []}, "decision_strategy": ""})
        self.assertFalse(
            service.manifest()["strategy_evolution"],
            "a payload without the switch must not silently re-enable it",
        )

    def test_a_non_boolean_switch_is_rejected(self) -> None:
        service = self._service()
        with self.assertRaises(ValueError):
            service.save_enabled(
                {"enabled": {"api": []}, "decision_strategy": "", "strategy_evolution": "yes"}
            )


if __name__ == "__main__":
    unittest.main()
