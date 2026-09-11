"""Business risk sees every market API call, whoever made it.

The guard wraps the API plugin rather than sitting in the Agent path, so a settlement sweep or a
manually triggered cycle is filtered exactly like a model-proposed trade. Reads are filtered too:
they spend the platform's rate budget and decide what the rest of the cycle gets to see.
"""
from __future__ import annotations

import unittest

from prediction_market_agent.core.risk import RiskCoordinator, RuleDecision
from prediction_market_agent.runtime.market_guard import (
    READ_OPERATIONS,
    GuardedMarketApi,
    MarketActionRejected,
)


class FakePlugin:
    name = "binance"
    capabilities = ("read", "write")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gateway = type("Gateway", (), {})()

    def _record(self, name, result=None):
        self.calls.append(name)
        return result

    def sync_time(self): return self._record("sync_time")
    def list_topics(self, *, offset, limit): return self._record("list_topics", [])
    def get_topic(self, topic_id): return self._record("get_topic", {"id": topic_id})
    def get_order_book(self, market_id, outcome_id): return self._record("get_order_book", {})
    def get_candles(self, reference_symbol, interval="1m", limit=120):
        return self._record("get_candles", [])
    def search_market_candidates(self, query, limit): return self._record("search", [])
    def create_write_gateway(self, state): return self.gateway
    def plugin_specific_extra(self, value): return self._record("extra", value)


class Recorder:
    target = "market:*"

    def __init__(self, outcome="ALLOW", *, value=None):
        self.outcome = outcome
        self.value = value
        self.seen: list[tuple[str, dict]] = []

    def evaluate(self, operation, context):
        self.seen.append((operation, context))
        return RuleDecision(self.outcome, f"recorder said {self.outcome}", self.value)

    def manifest(self): return {"target": self.target}


class MarketGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = FakePlugin()
        self.rule = Recorder()
        self.risk = RiskCoordinator()
        self.risk.register(self.rule)
        self.guard = GuardedMarketApi(self.plugin, self.risk)

    def invoke(self, operation: str) -> None:
        {
            "sync_time": lambda: self.guard.sync_time(),
            "list_topics": lambda: self.guard.list_topics(offset=0, limit=10),
            "get_topic": lambda: self.guard.get_topic("t-1"),
            "get_order_book": lambda: self.guard.get_order_book("m-1", "o-1"),
            "get_candles": lambda: self.guard.get_candles("BTCUSDT"),
            "search_market_candidates": lambda: self.guard.search_market_candidates("btc", 5),
        }[operation]()

    def test_every_read_in_the_contract_is_filtered(self) -> None:
        for operation in READ_OPERATIONS:
            with self.subTest(operation=operation):
                self.rule.seen.clear()
                self.invoke(operation)
                self.assertEqual([name for name, _ in self.rule.seen], [operation])

    def test_a_refused_read_never_reaches_the_platform(self) -> None:
        guard = GuardedMarketApi(self.plugin, self._coordinator("REJECT"))
        with self.assertRaises(MarketActionRejected):
            guard.get_order_book("m-1", "o-1")
        self.assertEqual(self.plugin.calls, [], "the platform must not be called after a refusal")

    def test_a_halt_also_stops_the_call(self) -> None:
        guard = GuardedMarketApi(self.plugin, self._coordinator("HALT"))
        with self.assertRaises(MarketActionRejected):
            guard.list_topics(offset=0, limit=1)

    def test_a_shrink_request_refuses_rather_than_passing_the_call_at_full_size(self) -> None:
        """A quote's size is already bound and a read has none, so ADJUST cannot be honoured here.

        Passing it through would leave the rule author believing a cap applied while the full order
        went out - the failure mode that turns a missing guard into money leaving.
        """
        guard = GuardedMarketApi(self.plugin, self._coordinator("ADJUST", value=10.0))
        with self.assertRaises(MarketActionRejected) as caught:
            guard.create_write_gateway(object()).business_risk(
                "place_order", {"notional": 100}
            )
        self.assertIn("agent_policy", str(caught.exception), "say where reducing does work")
        self.assertEqual(self.plugin.calls, [])

    def test_an_allowed_read_returns_the_platform_result_unchanged(self) -> None:
        self.assertEqual(self.guard.get_topic("t-9"), {"id": "t-9"})

    def test_rules_are_told_which_platform_and_that_this_is_a_market_call(self) -> None:
        self.guard.get_candles("BTCUSDT")
        _, context = self.rule.seen[0]
        self.assertEqual(context["kind"], "market_api")
        self.assertEqual(context["platform"], "binance")
        self.assertEqual(context["protected_target"], "market:binance")

    def test_the_target_is_keyed_by_platform_so_rules_can_scope_to_one(self) -> None:
        scoped = Recorder()
        scoped.target = "market:polymarket"
        coordinator = RiskCoordinator()
        coordinator.register(scoped)
        GuardedMarketApi(self.plugin, coordinator).sync_time()
        self.assertEqual(scoped.seen, [], "a polymarket rule must not fire on a binance call")

    def test_the_framework_still_sees_the_plugin_identity_through_the_guard(self) -> None:
        self.assertEqual(self.guard.name, "binance")
        self.assertEqual(self.guard.capabilities, ("read", "write"))

    def test_methods_beyond_the_contract_pass_through_ungated(self) -> None:
        """A plugin offering more than the contract keeps working; this layer just does not gate it."""
        self.assertEqual(self.guard.plugin_specific_extra(7), 7)
        self.assertEqual(self.rule.seen, [])

    def test_the_write_gateway_is_handed_the_same_check(self) -> None:
        gateway = self.guard.create_write_gateway(object())
        self.assertIs(gateway, self.plugin.gateway)
        gateway.business_risk("place_order", {"notional": 10})
        self.assertEqual([name for name, _ in self.rule.seen], ["place_order"])

    def test_a_write_refused_through_the_gateway_raises(self) -> None:
        guard = GuardedMarketApi(self.plugin, self._coordinator("REJECT"))
        gateway = guard.create_write_gateway(object())
        with self.assertRaises(MarketActionRejected):
            gateway.business_risk("transfer", {"amount": 5})

    def _coordinator(self, outcome: str, *, value: float | None = None) -> RiskCoordinator:
        coordinator = RiskCoordinator()
        coordinator.register(Recorder(outcome, value=value))
        return coordinator


if __name__ == "__main__":
    unittest.main()
