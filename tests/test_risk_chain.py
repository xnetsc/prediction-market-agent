"""The two filter categories stack, and a chain is a conjunction.

Both agent-policy and business-risk plugins may be enabled several at a time. Each enabled plugin
sees the action and any one of them can fail it, so these tests pin the properties an operator
relies on when combining a broad house rule with a narrow one.
"""
from __future__ import annotations

import unittest

from prediction_market_agent.core.risk import RiskCoordinator, RuleDecision


class Recorder:
    """A filter that answers as instructed and remembers whether it was consulted."""

    def __init__(self, target: str, outcome: str = "ALLOW", *, value: float | None = None,
                 raises: BaseException | None = None):
        self.target = target
        self.outcome = outcome
        self.value = value
        self.raises = raises
        self.seen: list[str] = []

    def evaluate(self, operation: str, context: dict) -> RuleDecision:
        self.seen.append(operation)
        if self.raises is not None:
            raise self.raises
        return RuleDecision(self.outcome, f"{self.target} said {self.outcome}", self.value)

    def manifest(self) -> dict:
        return {"target": self.target}


class ChainTests(unittest.TestCase):
    def coordinator(self, *engines) -> RiskCoordinator:
        coordinator = RiskCoordinator()
        for engine in engines:
            coordinator.register(engine)
        return coordinator

    def test_several_plugins_of_one_category_may_guard_the_same_target(self) -> None:
        first, second = Recorder("agent:actions"), Recorder("agent:actions")
        decision = self.coordinator(first, second).evaluate("agent:actions", "BUY", {})
        self.assertEqual(decision.outcome, "ALLOW")
        self.assertEqual((first.seen, second.seen), (["BUY"], ["BUY"]))

    def test_one_refusal_fails_the_action_however_many_others_allowed_it(self) -> None:
        for refusing in ("REJECT", "HALT"):
            with self.subTest(outcome=refusing):
                allowing = Recorder("market:*")
                refuser = Recorder("market:*", refusing)
                decision = self.coordinator(allowing, refuser).evaluate(
                    "market:binance", "place_order", {}
                )
                self.assertEqual(decision.outcome, refusing)

    def test_a_refusal_stops_the_chain_so_later_plugins_never_run(self) -> None:
        """Later plugins may be operator-supplied Python; the verdict is already settled."""
        refuser = Recorder("market:*", "REJECT")
        downstream = Recorder("market:*")
        self.coordinator(refuser, downstream).evaluate("market:binance", "place_order", {})
        self.assertEqual(downstream.seen, [], "a decided action must not run further rules")

    def test_a_plugin_that_raises_refuses_rather_than_allowing(self) -> None:
        broken = Recorder("market:*", raises=RuntimeError("rule file is broken"))
        decision = self.coordinator(broken).evaluate("market:binance", "get_order_book", {})
        self.assertEqual(decision.outcome, "REJECT")
        self.assertIn("rule file is broken", decision.reason)

    def test_a_plugin_returning_a_nonsense_outcome_refuses(self) -> None:
        decision = self.coordinator(Recorder("agent:actions", "probably fine")).evaluate(
            "agent:actions", "BUY", {}
        )
        self.assertEqual(decision.outcome, "REJECT")

    def test_a_broken_plugin_does_not_silence_the_rest_of_the_chain(self) -> None:
        broken = Recorder("market:*", raises=RuntimeError("boom"))
        healthy = Recorder("market:*")
        decision = self.coordinator(healthy, broken).evaluate("market:binance", "redeem", {})
        self.assertEqual(decision.outcome, "REJECT")
        self.assertEqual(healthy.seen, ["redeem"])

    def test_a_glob_target_covers_every_platform(self) -> None:
        engine = Recorder("market:*", "REJECT")
        coordinator = self.coordinator(engine)
        for platform in ("market:binance", "market:polymarket"):
            with self.subTest(platform=platform):
                self.assertEqual(coordinator.evaluate(platform, "list_topics", {}).outcome, "REJECT")

    def test_a_plugin_only_sees_the_targets_it_declares(self) -> None:
        policy = Recorder("agent:actions", "REJECT")
        coordinator = self.coordinator(policy)
        self.assertEqual(coordinator.evaluate("market:binance", "get_candles", {}).outcome, "ALLOW")
        self.assertEqual(policy.seen, [])

    def test_the_strictest_adjustment_wins_over_the_whole_chain(self) -> None:
        decision = self.coordinator(
            Recorder("market:*", "ADJUST", value=80.0),
            Recorder("market:*", "ADJUST", value=25.0),
            Recorder("market:*", "ADJUST", value=50.0),
        ).evaluate("market:binance", "place_order", {"requested_value": 100.0})
        self.assertEqual(decision.outcome, "ADJUST")
        self.assertEqual(decision.adjusted_value, 25.0)

    def test_no_enabled_plugin_means_nothing_is_filtered(self) -> None:
        decision = RiskCoordinator().evaluate("agent:actions", "BUY", {"requested_value": 3})
        self.assertEqual(decision.outcome, "ALLOW")

    def test_plugins_are_consulted_in_the_order_they_were_enabled(self) -> None:
        order: list[str] = []

        class Ordered(Recorder):
            def evaluate(self, operation, context):
                order.append(self.target_name)
                return super().evaluate(operation, context)

        first, second = Ordered("market:*"), Ordered("market:*")
        first.target_name, second.target_name = "first", "second"
        self.coordinator(first, second).evaluate("market:binance", "sync_time", {})
        self.assertEqual(order, ["first", "second"])

    def test_duplicate_targets_each_get_their_own_manifest_entry(self) -> None:
        manifests = self.coordinator(
            Recorder("market:*"), Recorder("market:*"), Recorder("agent:actions")
        ).manifests()
        self.assertEqual(sorted(manifests), ["agent:actions", "market:*#1", "market:*#2"])


if __name__ == "__main__":
    unittest.main()


class RuleLaneTests(unittest.TestCase):
    """The same rule file loaded into either category must land on that category's target."""

    def test_each_category_loads_trusted_python_onto_its_own_target(self) -> None:
        from prediction_market_agent.plugins.agent_policy.custom_rules import AgentRuleEngine
        from prediction_market_agent.plugins.risk.custom_rules import BusinessRuleEngine

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "rule.py"
            path.write_text(
                "def evaluate(operation, context):\n"
                "    return {'outcome': 'ALLOW', 'reason': 'ok'}\n",
                encoding="utf-8",
            )
            self.assertEqual(BusinessRuleEngine(path, 0).target, "market:*")
            self.assertEqual(AgentRuleEngine(path, 1).target, "agent:actions")

    def test_a_rule_can_still_govern_agent_behaviour(self) -> None:
        """Splitting the categories must not drop the ability to filter tool calls with Python."""
        from prediction_market_agent.plugins.agent_policy.custom_rules import AgentRuleEngine

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "rule.py"
            path.write_text(
                "def evaluate(operation, context):\n"
                "    if context.get('kind') == 'tool' and operation == 'SEARCH_WEB':\n"
                "        return {'outcome': 'REJECT', 'reason': 'no web search'}\n"
                "    return {'outcome': 'ALLOW', 'reason': 'ok'}\n",
                encoding="utf-8",
            )
            coordinator = RiskCoordinator()
            coordinator.register(AgentRuleEngine(path, 0))
            blocked = coordinator.evaluate("agent:actions", "SEARCH_WEB", {"kind": "tool"})
            allowed = coordinator.evaluate("agent:actions", "READ_BOOK", {"kind": "tool"})
            self.assertEqual(blocked.outcome, "REJECT")
            self.assertEqual(allowed.outcome, "ALLOW")

    def test_an_adjust_without_a_value_refuses_rather_than_allowing(self) -> None:
        coordinator = RiskCoordinator()
        coordinator.register(Recorder("market:*", "ADJUST"))
        decision = coordinator.evaluate("market:binance", "place_order", {"requested_value": 100})
        self.assertEqual(decision.outcome, "REJECT")
        self.assertIn("adjusted_value", decision.reason)
