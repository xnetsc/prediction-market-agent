from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prediction_market_agent.agent.decision import (
    AgentRunResult,
    DecisionProviderError,
    FallbackDecisionProvider,
)
from prediction_market_agent.agent.provider_health import (
    COOLDOWN_SECONDS,
    ProviderHealthRegistry,
    classify_error,
)
from prediction_market_agent.runtime.memory import SessionMemory
from prediction_market_agent.runtime.provider_quality import ProviderQuality


class StubProvider:
    """Stands in for AgentDecisionProvider; fails until told otherwise."""

    def __init__(self, name: str, error: str | None = None):
        self.name = name
        self.error = error
        self.calls = 0

    def run(self, payload, **options):
        self.calls += 1
        if self.error:
            raise DecisionProviderError(self.error, "{}")
        return AgentRunResult(
            value={"ok": True, "by": self.name},
            raw_output="{}",
            provider=self.name,
            research_trace=[],
        )

    def decide(self, payload, **options):  # pragma: no cover - unused here
        raise AssertionError("not used")


class ErrorClassificationTests(unittest.TestCase):
    def test_failures_are_paced_by_what_actually_went_wrong(self) -> None:
        cases = {
            "Binance HTTP 429: too many requests": "rate_limit",
            "You have hit your usage limit; try again later": "rate_limit",
            "HTTP 401 unauthorized": "auth",
            "invalid api key": "auth",
            "Connection timed out": "transient",
            "HTTP 503 service unavailable": "transient",
            "Invalid decision payload: 'rationale'": "contract",
            "something nobody predicted": "unknown",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)
        self.assertGreater(
            COOLDOWN_SECONDS["rate_limit"],
            COOLDOWN_SECONDS["transient"],
            "a throttled account needs a longer rest than a dropped connection",
        )


class HealthRegistryTests(unittest.TestCase):
    def test_a_failing_provider_stops_being_tried_first(self) -> None:
        registry = ProviderHealthRegistry(("a", "b"))
        registry.record_failure("a", "HTTP 429 rate limit")
        self.assertEqual(registry.order(("a", "b")), ["b", "a"])

    def test_cooldown_grows_with_repeated_failures_and_clears_on_success(self) -> None:
        registry = ProviderHealthRegistry(("a",))
        registry.record_failure("a", "HTTP 429 rate limit")
        first = registry.state("a").cooldown_until
        registry.record_failure("a", "HTTP 429 rate limit")
        second = registry.state("a").cooldown_until
        self.assertGreater(second, first, "repeat failures must back off further")
        registry.record_success("a", latency_seconds=0.2)
        self.assertTrue(registry.state("a").available_at(0.0))
        self.assertEqual(registry.state("a").consecutive_failures, 0)

    def test_quality_orders_providers_that_are_all_healthy(self) -> None:
        registry = ProviderHealthRegistry(("a", "b", "c"))
        registry.set_quality({"a": 0.8, "b": 1.4, "c": 1.0})
        self.assertEqual(registry.order(("a", "b", "c")), ["b", "c", "a"])

    def test_every_provider_cooled_down_still_gets_tried(self) -> None:
        registry = ProviderHealthRegistry(("a", "b"))
        registry.record_failure("a", "429")
        registry.record_failure("b", "429")
        self.assertEqual(sorted(registry.order(("a", "b"))), ["a", "b"])


class FailoverTests(unittest.TestCase):
    def _provider(self, *stubs: StubProvider) -> FallbackDecisionProvider:
        return FallbackDecisionProvider(
            list(stubs), {}, tuple(item.name for item in stubs)
        )

    def test_a_throttled_provider_is_skipped_on_the_next_call(self) -> None:
        throttled = StubProvider("throttled", "HTTP 429 rate limit")
        healthy = StubProvider("healthy")
        provider = self._provider(throttled, healthy)

        first = provider.run({}, schema={}, schema_name="s", mission="m")
        self.assertEqual(first.provider, "healthy")
        self.assertEqual(throttled.calls, 1)

        provider.run({}, schema={}, schema_name="s", mission="m")
        self.assertEqual(
            throttled.calls, 1, "a cooled-down provider must not be retried every call"
        )
        self.assertEqual(healthy.calls, 2)

    def test_a_recovered_provider_returns_to_service(self) -> None:
        recovering = StubProvider("recovering", "HTTP 429 rate limit")
        healthy = StubProvider("healthy")
        provider = self._provider(recovering, healthy)
        provider.run({}, schema={}, schema_name="s", mission="m")

        recovering.error = None
        provider.health.state("recovering").cooldown_until = 0.0
        result = provider.run({}, schema={}, schema_name="s", mission="m")
        self.assertEqual(
            result.provider, "recovering", "recovery must put it back at the front"
        )

    def test_health_manifest_reports_why_a_provider_is_out(self) -> None:
        provider = self._provider(
            StubProvider("throttled", "HTTP 429 rate limit"), StubProvider("healthy")
        )
        provider.run({}, schema={}, schema_name="s", mission="m")
        rows = {item["provider"]: item for item in provider.health.manifest()}
        self.assertEqual(rows["throttled"]["last_error_kind"], "rate_limit")
        self.assertFalse(rows["throttled"]["available"])
        self.assertGreater(rows["throttled"]["cooldown_seconds_remaining"], 0)
        self.assertTrue(rows["healthy"]["available"])
        self.assertEqual(rows["healthy"]["success_rate"], 1.0)


class ProviderQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def _settle(self, provider: str, estimate: float, won: bool, count: int) -> None:
        for index in range(count):
            token = f"{provider}-{estimate}-{won}-{index}"
            decision_id = self.memory.begin_decision(
                platform="fake",
                market_topic_id=f"t{index}",
                market_id="m",
                token_id=token,
                strategy_name="built_in",
                strategy_sha256="x",
                context={},
            )
            self.memory.complete_decision(
                decision_id,
                provider=provider,
                proposed_decision={
                    "action": "BUY",
                    "estimated_probability": estimate,
                    "confidence": 0.6,
                    "priors": [],
                },
                status="OK",
            )
            self.memory.record_action(
                platform="fake",
                market_topic_id=f"t{index}",
                token_id=token,
                action="REDEEM",
                request={"winning": won, "outcome": "YES"},
                result={"ok": True},
            )

    def test_better_calibrated_provider_scores_higher(self) -> None:
        # "sharp" says 0.9 and is right; "vague" says 0.9 and is wrong.
        self._settle("sharp", 0.9, True, 30)
        self._settle("vague", 0.9, False, 30)
        quality = ProviderQuality(memory=self.memory, provider=None)
        measured = quality.measure()
        scores = quality.scores(measured)
        self.assertLess(measured["sharp"]["brier_score"], measured["vague"]["brier_score"])
        self.assertGreater(scores["sharp"], scores["vague"])

    def test_delivery_failures_pull_a_score_down(self) -> None:
        for _ in range(20):
            self.memory.record_turn(
                platform="p", provider="steady", market_topic_id="1", token_id="t",
                input_payload={}, raw_output="", decision={"action": "HOLD"}, status="OK",
            )
        for _ in range(20):
            self.memory.record_turn(
                platform="p", provider="flaky", market_topic_id="1", token_id="t",
                input_payload={}, raw_output="", decision=None, status="ERROR",
                error="HTTP 429 rate limit",
            )
        quality = ProviderQuality(memory=self.memory, provider=None)
        scores = quality.scores()
        self.assertGreater(scores["steady"], scores["flaky"])

    def test_a_provider_never_grades_itself(self) -> None:
        decision_id = self.memory.begin_decision(
            platform="p", market_topic_id="1", market_id="m", token_id="t",
            strategy_name="s", strategy_sha256="x", context={},
        )
        self.memory.complete_decision(
            decision_id, provider="alpha",
            proposed_decision={"action": "HOLD", "estimated_probability": 0.5},
            status="OK",
        )
        self.memory.record_provider_review(
            decision_id=decision_id, subject_provider="alpha",
            reviewer_provider="alpha", scores={"overall": 1.0},
        )
        self.assertEqual(
            self.memory.provider_review_scores(), {},
            "self-graded reviews must not count toward quality",
        )
        self.memory.record_provider_review(
            decision_id=decision_id, subject_provider="alpha",
            reviewer_provider="beta", scores={"overall": 0.4},
        )
        self.assertEqual(self.memory.provider_review_scores()["alpha"]["average"], 0.4)

    def test_cross_evaluation_routes_to_a_different_provider(self) -> None:
        decision_id = self.memory.begin_decision(
            platform="p", market_topic_id="1", market_id="m", token_id="t",
            strategy_name="s", strategy_sha256="x", context={"order_book": {"best_bid": 0.4}},
        )
        self.memory.complete_decision(
            decision_id, provider="alpha",
            proposed_decision={"action": "BUY", "estimated_probability": 0.7},
            status="OK",
        )

        class Grader(StubProvider):
            def run(self, payload, **options):
                self.calls += 1
                return AgentRunResult(
                    value={
                        "grounded": 0.9, "consistent": 0.8, "cost_aware": 0.7,
                        "overall": 0.8, "notes": "checked against the supplied book",
                    },
                    raw_output="{}", provider=self.name, research_trace=[],
                )

        alpha, beta = Grader("alpha"), Grader("beta")
        provider = FallbackDecisionProvider([alpha, beta], {}, ("alpha", "beta"))
        graded = ProviderQuality(memory=self.memory, provider=provider).cross_evaluate(
            sample=5
        )
        self.assertEqual(graded, 1)
        self.assertEqual(beta.calls, 1)
        self.assertEqual(alpha.calls, 0, "alpha's own decision must not be graded by alpha")
        self.assertEqual(self.memory.provider_review_scores()["alpha"]["average"], 0.8)


if __name__ == "__main__":
    unittest.main()
