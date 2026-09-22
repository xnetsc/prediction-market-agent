from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
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
from prediction_market_agent.runtime.market_discovery import DiscoveryEngine, topic_features
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
        self.tool_descriptions: dict = {}
        self.tools = tools

    def run(self, payload, **options):
        self.requests.append(payload)
        self.instructions = options.get("instructions", "")
        self.tool_descriptions = options.get("tool_descriptions") or {}
        executor = options.get("tool_executor")
        for name in self.tools:
            first = payload["candidates"][0]
            arguments = (
                {"topic_id": first["topic_id"]}
                if name != "OUTCOME_BOOK"
                else {"market_id": f"m{first['topic_id']}", "outcome_id": "yes"}
            )
            self.tool_results.append(executor(name, arguments))
        picks = payload["candidates"][:5]
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
            platform="fake", plugin=plugin
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
        first = engine.discover(platform="fake", plugin=plugin)
        second = engine.discover(platform="fake", plugin=plugin)
        self.assertTrue(first and second)
        self.assertFalse(
            {topic.topic_id for topic in first} & {topic.topic_id for topic in second},
            "topics just analyzed must not immediately win the next slate",
        )

    def test_agent_drives_reads_through_plugin_interfaces_within_budget(self) -> None:
        """Prefetch and the agent draw on one allowance, so the platform is read once for it."""
        plugin = FakePlugin(30)
        budget = BuiltInMarketDiscovery().budget()
        provider = RecordingProvider(tools=("TOPIC_DETAIL", "OUTCOME_BOOK", "TOPIC_HISTORY"))
        self._engine(provider).discover(platform="fake", plugin=plugin)
        self.assertLessEqual(plugin.detail_calls, budget.detail_read_safety_limit)
        self.assertLessEqual(plugin.book_calls, budget.book_read_safety_limit)
        self.assertIn("MARKET_DISCOVERY_STRATEGY", provider.instructions)
        self.assertIn("MISSION", provider.instructions)

    def test_the_gates_are_read_for_the_candidates_the_round_names_in_one_step(self) -> None:
        """Which candidates are worth a read is the round's judgement; one step reads them all."""
        plugin = FakePlugin(30)

        class Verifier(RecordingProvider):
            steps = 0

            def run(inner, payload, **options):
                inner.steps += 1
                ids = [item["topic_id"] for item in payload["candidates"][:6]]
                inner.verified = options["tool_executor"]("VERIFY_TOPICS", {"topic_ids": ids})
                return super().run(payload, **options)

        provider = Verifier()
        self._engine(provider).discover(platform="fake", plugin=plugin)
        candidates = provider.requests[0]["candidates"]
        self.assertTrue(candidates)
        self.assertFalse(any("spread" in item for item in candidates),
                         "the list arrives unpriced; reading it is the round's call")
        for field in ("question", "description", "rank_suggestion"):
            self.assertIn(field, candidates[0], f"the list has to be judgeable on {field}")
        verified = provider.verified["verified"]
        self.assertEqual(len(verified), 6, "one step, six candidates")
        self.assertEqual(provider.steps, 1)
        entry = next(iter(verified.values()))
        self.assertIn("seconds_remaining", entry)
        self.assertTrue(any("spread" in item for item in verified.values()),
                        "the cost gate is about the round trip, which this has to supply")

    def test_a_round_that_cannot_ask_a_model_selects_nothing(self) -> None:
        """No formula stands in for the judgement. It used to order the pool by prescore and trade
        it, so rounds nobody reasoned about were recorded under a strategy that had not run - and
        that happened exactly when something was already wrong with the models."""
        plugin = FakePlugin(20)
        selected = self._engine(LegacyProvider()).discover(platform="fake", plugin=plugin)
        self.assertEqual(list(selected), [], "nothing is selected without a model choosing it")
        strategies = {
            row[0]
            for row in self.memory.connection.execute(
                "SELECT DISTINCT strategy FROM discovery_selections"
            )
        }
        self.assertNotIn("built_in:mechanical", strategies)
        incident = self.memory.connection.execute(
            "SELECT stage, severity, fallback FROM runtime_incidents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(incident[0], "market_selection")
        self.assertEqual(incident[1], "error")
        self.assertIn("不按公式代替模型判断", incident[2])

    def test_budget_refuses_reads_beyond_the_configured_allowance(self) -> None:
        plugin = FakePlugin(30)
        budget = BuiltInMarketDiscovery().budget()
        provider = RecordingProvider(tools=("TOPIC_DETAIL",) * (budget.detail_read_safety_limit + 2))
        self._engine(provider).discover(platform="fake", plugin=plugin)
        refused = [item for item in provider.tool_results if not item.get("ok")]
        self.assertEqual(plugin.detail_calls, budget.detail_read_safety_limit)
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


class EveryModelCallIsRecordedTests(unittest.TestCase):
    """A model was asked and the ledger says nothing: an operator cannot tell that from idle."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    def _rows(self) -> list[dict]:
        cursor = self.memory.connection.execute(
            "SELECT platform, strategy_name, status, error, final_decision_json"
            " FROM decision_ledger ORDER BY id"
        )
        return [
            {"platform": r[0], "strategy": r[1], "status": r[2], "error": r[3], "final": r[4]}
            for r in cursor
        ]

    def _discover(self, provider) -> None:
        DiscoveryEngine(
            memory=self.memory, provider=provider, strategy=BuiltInMarketDiscovery(),
            evolution_enabled=True,
        ).discover(platform="fake", plugin=FakePlugin(30))

    def test_a_round_that_selected_something_is_recorded(self) -> None:
        self._discover(RecordingProvider())
        [row] = self._rows()
        self.assertEqual(row["status"], "OK")
        self.assertIn("discovery", row["strategy"])

    def test_a_round_that_selected_nothing_is_recorded_with_its_reason(self) -> None:
        class Declines(RecordingProvider):
            def run(self, payload, **options):
                super().run(payload, **options)
                return AgentRunResult(
                    value={"selections": [], "skipped_reason": "nothing cleared the cost gate"},
                    raw_output="{}", provider="declines", research_trace=[],
                )

        self._discover(Declines())
        [row] = self._rows()
        self.assertEqual(row["status"], "NO_ACTION")
        self.assertIn("cost gate", row["error"])

    def test_a_round_whose_model_failed_is_recorded_as_a_failure(self) -> None:
        """Falling back to prescore order is a decision too, and it used to leave no trace."""
        self._discover(LegacyProvider())
        [row] = self._rows()
        self.assertEqual(row["status"], "PROVIDER_ERROR")
        self.assertTrue(row["error"], "the reason the model could not be used has to be kept")


class DiscoveryCanFillItsOwnGapsTests(unittest.TestCase):
    """Saying "no data" about something never looked up is not a finding, it is an unspent tool."""

    class _Research:
        descriptions = {
            "SEARCH_WEB": {"purpose": "search", "arguments": {}},
            "FETCH_URL": {"purpose": "read a page", "arguments": {}},
        }

        def create(self, context):
            del context
            return self

        def execute(self, name, arguments):
            return {"ok": True, "tool": name, "arguments": arguments}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    def _engine(self, provider, research):
        return DiscoveryEngine(
            memory=self.memory, provider=provider, strategy=BuiltInMarketDiscovery(),
            evolution_enabled=True, research_contributions=research,
        )

    def test_the_open_web_is_offered_to_discovery_not_only_to_decisions(self) -> None:
        provider = RecordingProvider()
        self._engine(provider, [self._Research()]).discover(
            platform="fake", plugin=FakePlugin(20)
        )
        offered = provider.tool_descriptions or {}
        for tool in ("SEARCH_WEB", "FETCH_URL"):
            self.assertIn(tool, offered, f"discovery cannot go and find anything without {tool}")

    def test_a_research_tool_actually_runs_from_discovery(self) -> None:
        provider = RecordingProvider(tools=("SEARCH_WEB",))
        self._engine(provider, [self._Research()]).discover(
            platform="fake", plugin=FakePlugin(20)
        )
        used = [r for r in provider.tool_results if r.get("tool") == "SEARCH_WEB"]
        self.assertTrue(used and used[0]["ok"], f"the tool was offered but not usable: {provider.tool_results}")

    def test_without_research_plugins_discovery_still_works(self) -> None:
        provider = RecordingProvider()
        selected = self._engine(provider, []).discover(
            platform="fake", plugin=FakePlugin(20)
        )
        self.assertTrue(selected)
        self.assertNotIn("SEARCH_WEB", provider.tool_descriptions or {})

    def test_the_strategy_says_missing_evidence_is_something_to_go_and_get(self) -> None:
        text = BuiltInMarketDiscovery().instructions
        self.assertIn("MISSING EVIDENCE IS A TASK", text)
        self.assertIn("is not a reason", text)


class OneBadCandidateDoesNotEndTheRoundTests(unittest.TestCase):
    """A venue that will not describe one market is not a reason to abandon twenty-three others."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    def _plugin_that_refuses(self, failing: str):
        plugin = FakePlugin(20)
        real_book = plugin.get_order_book
        real_detail = plugin.get_topic

        def book(market_id, outcome_id):
            if failing == "book":
                raise RuntimeError('Polymarket read HTTP 404: no orderbook for this token')
            return real_book(market_id, outcome_id)

        def detail(topic_id):
            if failing == "detail":
                raise RuntimeError("Polymarket read HTTP 500")
            return real_detail(topic_id)

        plugin.get_order_book = book
        plugin.get_topic = detail
        return plugin

    def _discover(self, plugin):
        provider = RecordingProvider()
        selected = DiscoveryEngine(
            memory=self.memory, provider=provider, strategy=BuiltInMarketDiscovery(),
            evolution_enabled=True,
        ).discover(platform="fake", plugin=plugin)
        return selected, provider

    def test_a_market_with_no_order_book_still_leaves_a_round(self) -> None:
        selected, _provider = self._discover(self._plugin_that_refuses("book"))
        self.assertTrue(selected, "the whole cycle was abandoned over one unreadable book")
        self.assertTrue(any("book_error" in entry for entry in self._read(self._plugin_that_refuses("book"))))

    def test_a_topic_that_cannot_be_read_is_marked_unverified_not_fatal(self) -> None:
        selected, _provider = self._discover(self._plugin_that_refuses("detail"))
        self.assertTrue(selected)
        entries = self._read(self._plugin_that_refuses("detail"))
        self.assertTrue(all(not entry.get("verified") for entry in entries))
        self.assertTrue(any("lookup_error" in entry for entry in entries))

    def _read(self, plugin):
        """What the round learns when it asks about a venue that refuses part of the read."""
        from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox

        toolbox = _DiscoveryToolbox(
            plugin=plugin, memory=self.memory, platform="fake",
            budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
        )
        return list(toolbox.verify([topic.topic_id for topic in plugin.topics[:3]]).values())


class TheAgentSetsTheNextLookTests(unittest.TestCase):
    """When to come back and what to look for are judgements about the venue, not constants."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    class _Planner(RecordingProvider):
        plan = {"next_scan_seconds": 900, "next_survey_queries": ["fed decision"],
                "pacing_reason": "nothing due here until Thursday"}

        def run(self, payload, **options):
            super().run(payload, **options)
            head = (payload.get("candidates") or [])[:1]
            return AgentRunResult(
                value={
                    "selections": [
                        {"topic_id": item["topic_id"], "reason": "r", "priors": []}
                        for item in head
                    ],
                    "skipped_reason": "",
                    **self.plan,
                },
                raw_output="{}", provider="planner", research_trace=[],
            )

    def _engine(self, provider):
        return DiscoveryEngine(
            memory=self.memory, provider=provider, strategy=BuiltInMarketDiscovery(),
            evolution_enabled=True,
        )

    def test_the_schema_asks_for_both(self) -> None:
        from prediction_market_agent.runtime.market_discovery import DISCOVERY_SCHEMA

        for field in ("next_scan_seconds", "next_survey_queries", "pacing_reason"):
            self.assertIn(field, DISCOVERY_SCHEMA["required"])

    def test_what_it_asked_for_is_kept(self) -> None:
        self._engine(self._Planner()).discover(
            platform="fake", plugin=FakePlugin(20)
        )
        plan = self.memory.survey_plan("fake")
        self.assertEqual(plan["next_scan_seconds"], 900)
        self.assertEqual(plan["queries"], ["fed decision"])
        self.assertIn("Thursday", plan["reason"])

    def test_the_next_survey_looks_for_what_it_named(self) -> None:
        plugin = FakePlugin(20)
        asked: list[str] = []
        plugin.search_market_candidates = lambda query, limit: asked.append(query) or []
        self.memory.save_survey_plan(
            platform="fake", queries=["opec meeting"], next_scan_seconds=0, reason="",
        )
        self._engine(RecordingProvider()).discover(
            platform="fake", plugin=plugin
        )
        self.assertEqual(asked, ["opec meeting"])

    def test_a_query_that_finds_nothing_leaves_the_round_intact(self) -> None:
        plugin = FakePlugin(20)
        plugin.search_market_candidates = lambda query, limit: []
        self.memory.save_survey_plan(
            platform="fake", queries=["nothing matches this"], next_scan_seconds=0, reason="",
        )
        selected = self._engine(RecordingProvider()).discover(
            platform="fake", plugin=plugin
        )
        self.assertTrue(selected, "the listing must still be surveyed")

    def test_a_failing_search_does_not_end_the_round(self) -> None:
        plugin = FakePlugin(20)

        def explode(query, limit):
            raise RuntimeError("search endpoint is down")

        plugin.search_market_candidates = explode
        self.memory.save_survey_plan(
            platform="fake", queries=["anything"], next_scan_seconds=0, reason="",
        )
        self.assertTrue(
            self._engine(RecordingProvider()).discover(
                platform="fake", plugin=plugin
            )
        )

    def test_the_strategy_explains_that_it_cannot_ask_to_come_back_sooner(self) -> None:
        text = BuiltInMarketDiscovery().instructions
        self.assertIn("SET THE NEXT LOOK", text)
        self.assertIn("never to come back sooner", text)


class DeadlineLadderTests(unittest.TestCase):
    """An event asked across deadlines lists its earliest - usually closed - market first."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    def _ladder_plugin(self, open_markets: int = 1, total: int = 10):
        plugin = FakePlugin(4)
        asked: list[str] = []

        def get_topic(topic_id):
            plugin.detail_calls += 1
            topic = next(item for item in plugin.topics if item.topic_id == topic_id)
            markets = []
            for index in range(total):
                is_open = index >= total - open_markets
                markets.append(Market(
                    f"m{topic_id}-{index}", f"by month {index}", f"by month {index}?",
                    "OPEN" if is_open else "CLOSED",
                    50.0 * (index + 1), 50.0,
                    (Outcome(f"yes-{topic_id}-{index}", "Yes", 0.4),),
                ))
            return TopicDetail(
                topic=topic, start_time_ms=0,
                end_time_ms=int(time.time() * 1000) + 86_400_000, fee_bps=10,
                markets=tuple(markets),
            )

        def get_order_book(market_id, outcome_id):
            plugin.book_calls += 1
            asked.append(market_id)
            if not market_id.endswith(f"-{total - 1}") and open_markets == 1:
                raise RuntimeError("Polymarket read HTTP 404: No orderbook exists")
            return OrderBook((PriceLevel(0.39, 10),), (PriceLevel(0.41, 10),), 0)

        plugin.get_topic = get_topic
        plugin.get_order_book = get_order_book
        return plugin, asked

    def _candidates(self, plugin):
        """What the round learns when it asks about every candidate it was shown."""
        from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox

        toolbox = _DiscoveryToolbox(
            plugin=plugin, memory=self.memory, platform="fake",
            budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
        )
        return list(toolbox.verify([topic.topic_id for topic in plugin.topics]).values())

    def test_the_open_market_is_priced_even_when_it_is_listed_last(self) -> None:
        plugin, asked = self._ladder_plugin(open_markets=1, total=10)
        candidates = self._candidates(plugin)
        priced = [item for item in candidates if "spread" in item]
        self.assertTrue(priced, "the live market was never priced")
        self.assertFalse(any("book_error" in item for item in candidates),
                         "a closed market was asked for a book")
        self.assertTrue(all(market_id.endswith("-9") for market_id in asked))

    def test_it_says_which_deadline_the_price_belongs_to(self) -> None:
        plugin, _ = self._ladder_plugin(open_markets=1, total=10)
        priced = next(item for item in self._candidates(plugin) if "spread" in item)
        self.assertIn("by month 9", priced["priced_market"])
        self.assertEqual(priced["markets"], 10)
        self.assertEqual(priced["markets_open"], 1)

    def test_an_event_with_nothing_open_says_so_instead_of_asking(self) -> None:
        plugin, asked = self._ladder_plugin(open_markets=0, total=5)
        candidates = self._candidates(plugin)
        self.assertEqual(asked, [], "no closed market should ever be asked for a book")
        verified = [item for item in candidates if item.get("verified")]
        self.assertTrue(verified)
        self.assertTrue(all(item.get("tradeable") is False for item in verified))

    def test_the_agent_sees_live_markets_first_even_past_eight(self) -> None:
        """Cutting the venue's order at eight dropped the live markets of a long ladder entirely."""
        plugin, _ = self._ladder_plugin(open_markets=2, total=12)
        from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox

        toolbox = _DiscoveryToolbox(
            plugin=plugin, memory=self.memory, platform="fake",
            budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
        )
        detail = toolbox.execute("TOPIC_DETAIL", {"topic_id": plugin.topics[0].topic_id})
        self.assertEqual([m["status"] for m in detail["markets"][:2]], ["OPEN", "OPEN"])
        self.assertEqual(detail["markets_total"], 12)
        self.assertEqual(detail["markets_open"], 2)


class ShortDatedSmallAndOutAgainTests(unittest.TestCase):
    """The built-in strategy works a small amount of money through quick trades."""

    def test_both_strategies_carry_the_operators_horizon(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        discovery = BuiltInMarketDiscovery(horizon_days=3)
        self.assertIn("prefers what settles within 3 days", discovery.instructions)
        decision = BuiltInDecisionStrategy(horizon_days=3, max_trade_usdt=25)
        text = decision.instructions.replace("\n", " ")
        self.assertIn("prefers outcomes settling within 3 days", text)
        self.assertIn("bought at or under 25 USDT", text)
        # Preferences about method, which money may overrule with a reason on the record.
        self.assertIn("The objective is money", text)
        self.assertIn("say concretely why this opportunity is worth it", text)
        self.assertNotIn("Settling later than 3 days is a HOLD", text)

    def test_changing_the_setting_changes_the_text_and_its_hash(self) -> None:
        """The hash is what the ledger records, so it has to follow what the model was told."""
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        near, far = BuiltInDecisionStrategy(), BuiltInDecisionStrategy(horizon_days=30, max_trade_usdt=500)
        self.assertIn("30 days", far.instructions)
        self.assertIn("500 USDT", far.instructions)
        self.assertNotEqual(near.sha256, far.sha256)

    def test_the_settings_reach_the_strategies_that_run(self) -> None:
        source = Path("src/prediction_market_agent/runtime/bootstrap.py").read_text()
        self.assertIn("horizon_days=config.strategy_horizon_days", source)
        self.assertIn("max_trade_usdt=config.strategy_max_trade_usdt", source)

    def test_a_distant_settlement_still_reaches_the_round(self) -> None:
        """The window is the operator's preference about method, not a rule about what exists."""
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=_HorizonPlatform())
        request = provider.requests[0]
        self.assertEqual(len(request["candidates"]), 4, "nothing is withheld from the round")
        preferences = request["operator_preferences"]
        self.assertEqual(preferences["settles_within_days"], 3)
        self.assertIn("concrete reason", preferences["may_be_exceeded"])

    def test_what_settles_soon_is_suggested_first(self) -> None:
        """A five-minute crypto market has no volume next to an election; ranked together it sinks."""
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=_NearAndFarPlatform())
        candidates = provider.requests[0]["candidates"]
        self.assertEqual(candidates[0]["topic_id"], "near-1")
        self.assertTrue(candidates[0]["settles_inside_preferred_window"])
        self.assertIsNone(candidates[-1]["settles_inside_preferred_window"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")


_HORIZONS = {"1": 2 * 86400, "2": 12 * 3600, "3": 40 * 86400, "4": 400 * 86400}


class _NearAndFarPlatform(FakePlugin):
    """Two crowded markets months out, one small one settling tonight."""

    def __init__(self) -> None:
        super().__init__(topics=2)
        for topic in self.topics:
            object.__setattr__(topic, "volume_usdt", 5_000_000.0)
        self.near = Topic("near-1", "Game tonight", "q", "d", "sport", "OPEN", 9_000.0, 40.0, "n1")

    def list_topics_by_deadline(self, *, offset, limit, after_ms, before_ms):
        return TopicPage((self.near,) if offset == 0 else (), False, 1)


class _HorizonPlatform(FakePlugin):
    """Two markets settling within days, two settling months out."""

    def __init__(self) -> None:
        super().__init__(topics=4)

    def get_topic(self, topic_id: str):
        detail = super().get_topic(topic_id)
        return replace(detail, end_time_ms=int(time.time() * 1000) + _HORIZONS[topic_id] * 1000)


class WhenToComeBackTests(unittest.TestCase):
    """The interval is the soonest moment something could change what the robot would do."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    def test_the_round_is_told_what_moves_the_interval(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        text = BuiltInMarketDiscovery(horizon_days=3).instructions
        for factor in (
            "Deadlines already in range",
            "Markets about to come into range",
            "Dated catalysts",
            "How fast this venue is actually repricing",
            "When the resolving source publishes",
            "What is already open here",
            "Whether this venue lists anything new",
            "What the last few rounds produced",
            "What is happening outside this venue",
        ):
            with self.subTest(factor=factor):
                self.assertIn(factor, text)
        self.assertIn("A survey is not free", text)
        self.assertIn("the preferred window is in the request", text.replace("\n    ", " "))
        # Markets are listed late; the ones worth naming are the ones the world has but the venue
        # does not yet - a tournament under way, an election days out.
        self.assertIn("a tournament in progress", text)
        self.assertIn("Read the news for the few events", text)

    def test_the_preferred_window_travels_with_the_request(self) -> None:
        """Pacing around markets entering the window needs to know where the window is."""
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=_HorizonPlatform())
        request = provider.requests[0]
        self.assertEqual(request["operator_preferences"]["settles_within_days"], 3)
        self.assertIn("VERIFY_TOPICS", request["nothing_here_is_priced_yet"])


class _DistantPlatform(FakePlugin):
    """Everything settles three months out, and asking by date confirms it."""

    def __init__(self) -> None:
        super().__init__(topics=3)

    def get_topic(self, topic_id: str):
        detail = super().get_topic(topic_id)
        return replace(detail, end_time_ms=int(time.time() * 1000) + 90 * 86400 * 1000)

    def list_topics_by_deadline(self, *, offset, limit, after_ms, before_ms):
        return TopicPage((), False, 0)


class TheVenueIsAskedForWhatSettlesSoonTests(unittest.TestCase):
    """A listing ordered by volume is the worst way to find what settles this week."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    class _Venue(FakePlugin):
        """Busy markets settle in a month; the near-dated ones answer only when asked by date."""

        def __init__(self) -> None:
            super().__init__(topics=3)
            self.deadline_calls: list[dict] = []
            self.near = [
                Topic("near-1", "Game tonight", "q", "d", "sport", "OPEN", 5_000.0, 10.0, "n1"),
                Topic("near-2", "Bitcoin 5 minute", "q", "d", "crypto", "OPEN", 5_000.0, 10.0, "n2"),
            ]

        def list_topics_by_deadline(self, *, offset, limit, after_ms, before_ms):
            self.deadline_calls.append({"offset": offset, "limit": limit,
                                        "window_days": round((before_ms - after_ms) / 86400 / 1000)})
            return TopicPage(tuple(self.near[offset:offset + limit]), False, offset + len(self.near))

    def test_what_settles_soon_is_fetched_first_and_the_listing_still_runs(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        venue = self._Venue()
        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=venue)
        self.assertEqual(venue.deadline_calls[0]["window_days"], 3, "the window is the horizon")
        observed = {row["market_topic_id"] for row in self.memory.connection.execute(
            "SELECT market_topic_id FROM topic_observations"
        ).fetchall() for row in [{"market_topic_id": row[0]}]}
        self.assertTrue({"near-1", "near-2"} <= observed, "the near-dated ones were never surveyed")
        self.assertTrue({"1", "2", "3"} <= observed, "the venue's own listing stopped being read")

    def test_a_venue_that_cannot_answer_by_date_is_unaffected(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=FakePlugin(topics=3))
        self.assertTrue(provider.requests, "the ordinary listing round stopped working")

    def test_an_event_whose_date_has_passed_reads_as_settled(self) -> None:
        """Nothing can be traded on it, and the round can see that the moment it reads it."""
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery
        from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox

        class Expired(FakePlugin):
            def __init__(self) -> None:
                super().__init__(topics=2)

            def get_topic(self, topic_id: str):
                detail = super().get_topic(topic_id)
                stamp = int(time.time() * 1000) + (86_400_000 if topic_id == "1" else -3_600_000)
                return replace(detail, end_time_ms=stamp)

        toolbox = _DiscoveryToolbox(
            plugin=Expired(), memory=self.memory, platform="fake",
            budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
        )
        verified = toolbox.verify(["1", "2"])
        self.assertGreater(verified["1"]["seconds_remaining"], 0)
        self.assertEqual(verified["2"]["seconds_remaining"], 0)


class TheRoundCanFetchItsOwnCandidatesTests(unittest.TestCase):
    """What to look at is the agent's call; the plugin only decides when a cycle runs."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "m.sqlite3")

    class _ListingOnlyVenue(FakePlugin):
        """Everything it lists settles in two months; what settles tonight is only findable."""

        def __init__(self) -> None:
            super().__init__(topics=2)
            self.soon = Topic("soon-1", "Game tonight", "q", "d", "sport", "OPEN", 9_000.0, 50.0, "s1")
            self.searched: list[str] = []

        def get_topic(self, topic_id: str):
            if topic_id == "soon-1":
                detail = super().get_topic("1")
                return replace(detail, topic=self.soon,
                               end_time_ms=int(time.time() * 1000) + 6 * 3600 * 1000)
            detail = super().get_topic(topic_id)
            return replace(detail, end_time_ms=int(time.time() * 1000) + 60 * 86400 * 1000)

        def search_market_candidates(self, query, limit):
            self.searched.append(query)
            return [SimpleNamespace(topic=self.soon)]

    class _Searcher(RecordingProvider):
        def run(self, payload, **options):
            super().run(payload, **options)
            answer = options["tool_executor"]("FIND_TOPICS", {"query": "settling tonight"})
            found = answer["topics"][0]["topic_id"]
            return AgentRunResult(
                value={"selections": [{"topic_id": found, "reason": "settles tonight", "priors": []}],
                       "skipped_reason": "", "next_scan_seconds": 0, "next_survey_queries": [],
                       "pacing_reason": "", "headline": "went and found one"},
                raw_output="{}", provider="searcher", research_trace=[],
            )

    def test_a_market_it_found_itself_can_be_selected(self) -> None:
        """An empty shortlist is a fact about the listing, and the round can correct it."""
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        venue = self._ListingOnlyVenue()
        provider = self._Searcher()
        selected = DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=venue)
        self.assertEqual(venue.searched, ["settling tonight"])
        self.assertEqual([topic.topic_id for topic in selected], ["soon-1"])

    def test_both_ways_of_fetching_are_offered_and_explained(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        provider = RecordingProvider()
        DiscoveryEngine(
            memory=self.memory, provider=provider,
            strategy=BuiltInMarketDiscovery(horizon_days=3), evolution_enabled=False,
        ).discover(platform="fake", plugin=self._ListingOnlyVenue())
        self.assertIn("TOPICS_BY_DEADLINE", provider.tool_descriptions)
        self.assertIn("FIND_TOPICS", provider.tool_descriptions)
        text = BuiltInMarketDiscovery(horizon_days=3).instructions
        self.assertIn("NOTHING HERE IS ORDERED OR PRICED FOR YOU", text)
        self.assertIn("rank_suggestion", text)
        self.assertIn("Going and getting more is an ordinary", text)
        for reason in ("the wrong reading for what this runtime trades",
                       "what you just read points elsewhere",
                       "merely acceptable",
                       "more of a kind to choose between"):
            with self.subTest(reason=reason):
                self.assertIn(reason, text)

    def test_a_platform_that_cannot_list_by_date_says_so_rather_than_failing(self) -> None:
        from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        toolbox = _DiscoveryToolbox(
            plugin=FakePlugin(topics=1), memory=self.memory, platform="fake",
            budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
        )
        answer = toolbox.execute("TOPICS_BY_DEADLINE", {"within_hours": 72})
        self.assertFalse(answer["ok"])
        self.assertFalse(answer["supported"])
        self.assertIn("FIND_TOPICS", answer["error"])


class AlreadyAnsweredTests(unittest.TestCase):
    """A market found fairly priced stays fairly priced, and must stop taking slots for it.

    Selection knew only when a topic was last picked and how often - which cannot tell "looked at
    and left alone deliberately" from "never examined". So the same handful came back every round,
    were decided the same way, and used up a budget meant for finding something new.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = SessionMemory(Path(self.directory.name) / "s.sqlite3")
        self.addCleanup(self.memory.connection.close)

    def _decide(self, topic_id: str, action: str, *, headline: str, revisit: str) -> None:
        decision_id = self.memory.begin_decision(
            platform="fake", market_topic_id=topic_id, market_id="m", token_id="o",
            strategy_name="general_agent", strategy_sha256="x", context={},
        )
        self.memory.complete_decision(
            decision_id, provider="p", status="NO_ACTION",
            final_decision={"action": action, "headline": headline, "revisit_when": revisit},
        )

    def test_the_last_verdict_and_its_trigger_reach_the_round(self) -> None:
        self._decide("t1", "HOLD", headline="观望：价差已反映", revisit="跌破 0.40 再问")
        history = self.memory.topic_verdict_history(platform="fake")
        self.assertEqual(history["t1"]["last_action"], "HOLD")
        self.assertEqual(history["t1"]["revisit_when"], "跌破 0.40 再问")
        self.assertEqual(history["t1"]["holds"], 1)

    def test_repeated_holds_are_counted_and_marked(self) -> None:
        for _ in range(3):
            self._decide("t1", "HOLD", headline="观望", revisit="跌破 0.40 再问")
        history = self.memory.topic_verdict_history(platform="fake")["t1"]
        self.assertEqual(history["holds"], 3)
        features = topic_features(
            Topic(topic_id="t1", title="T", question="", description="", category="",
                  status="OPEN", liquidity_usdt=1.0, volume_usdt=1.0),
            previous=None, attention={"last_selected_at": 0, "selections": 3},
            now_ms=int(time.time() * 1000), verdict=history,
        )
        self.assertEqual(features["previous_verdict"]["times_held"], 3)
        self.assertEqual(features["previous_verdict"]["revisit_when"], "跌破 0.40 再问")
        self.assertIn("verdict:held-repeatedly", features["buckets"])

    def test_a_market_nobody_decided_says_so_rather_than_inventing_a_verdict(self) -> None:
        features = topic_features(
            Topic(topic_id="t2", title="T", question="", description="", category="",
                  status="OPEN", liquidity_usdt=1.0, volume_usdt=1.0),
            previous=None, attention=None, now_ms=int(time.time() * 1000), verdict=None,
        )
        self.assertNotIn("previous_verdict", features)
        self.assertTrue(features["never_selected"])

    def test_the_round_is_told_what_to_do_with_a_repeat(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        text = " ".join(BuiltInMarketDiscovery().instructions.split())
        self.assertIn("previous_verdict", text)
        self.assertIn("revisit_when", text)
        self.assertIn("held twice", text)
        self.assertIn("Recurring families", text)

    def test_a_hold_has_to_name_what_would_change_it(self) -> None:
        from prediction_market_agent.agent.decision import DECISION_SCHEMA
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        self.assertIn("revisit_when", DECISION_SCHEMA["required"])
        text = " ".join(BuiltInDecisionStrategy().instructions.split())
        self.assertIn("revisit_when", text)
        self.assertIn("priced about right", text)
