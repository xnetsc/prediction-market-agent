"""The model can reach every market capability, and both filter lanes still apply to it."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from prediction_market_agent.core.risk import RiskCoordinator, RuleDecision
from prediction_market_agent.runtime.market_guard import GuardedMarketApi, MarketActionRejected
from prediction_market_agent.runtime.market_tools import DESCRIPTIONS, MarketToolset


class FakeBook:
    def __init__(self, bid, ask):
        self.bids = [SimpleNamespace(price=bid, size=10.0)] if bid else []
        self.asks = [SimpleNamespace(price=ask, size=10.0)] if ask else []


class FakePlugin:
    def __init__(self, name, bid=0.4, ask=0.6):
        self.name = name
        self.capabilities = SimpleNamespace(to_dict=lambda: {"read": True})
        self.calls = []
        self._bid, self._ask = bid, ask

    def sync_time(self): self.calls.append("sync_time")
    def list_topics(self, *, offset, limit):
        self.calls.append("list_topics")
        return SimpleNamespace(topics=[], has_more=False, next_offset=0)
    def get_topic(self, topic_id):
        self.calls.append("get_topic")
        return SimpleNamespace(topic_id=topic_id, markets=[])
    def get_order_book(self, market_id, outcome_id):
        self.calls.append("get_order_book")
        return FakeBook(self._bid, self._ask)


class FakeGateway:
    def __init__(self): self.calls = []
    def get_quote(self, **values):
        self.calls.append(("quote", values))
        return SimpleNamespace(quote_id="q1", **values)
    def place_order(self, quote, reason=""):
        self.calls.append(("order", reason))
        return SimpleNamespace(order_id="o1", status="FILLED")
    def cancel_orders(self, ids): self.calls.append(("cancel", ids)); return {"canceled": ids}
    def redeem(self, outcome_id, winning): self.calls.append(("redeem", outcome_id)); return {"ok": True}
    def transfer(self, direction, amount): self.calls.append(("transfer", direction)); return {"ok": True}


def platform(name, risk, *, bid=0.4, ask=0.6):
    plugin = FakePlugin(name, bid, ask)
    return SimpleNamespace(
        plugin=GuardedMarketApi(plugin, risk),
        gateway=FakeGateway(),
        state=SimpleNamespace(
            starting_capital=100.0, cash=100.0, exposure=0.0, equity=100.0,
            realized_pnl=0.0, transferred_out=0.0, positions={}, orders=[],
        ),
        raw=plugin,
    )


class ToolCoverageTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskCoordinator()
        self.platforms = {
            "binance": platform("binance", self.risk, bid=0.40, ask=0.42),
            "polymarket": platform("polymarket", self.risk, bid=0.55, ask=0.57),
        }
        self.tools = MarketToolset(self.platforms, "binance")

    def test_every_market_api_capability_has_a_tool(self) -> None:
        """Derived from the contract, so a new method without a tool fails here rather than silently
        leaving the model to guess across the gap."""
        from prediction_market_agent.plugin_system.contracts import PredictionMarketApiPlugin
        from prediction_market_agent.runtime.broker import ExecutionGateway

        # Framework plumbing rather than something the model would ever call.
        plumbing = {
            "create_write_gateway", "write_transport", "configuration_manifest",
            "topic_page_size", "capabilities", "name", "mark",
            "cycle_limits",      # how often the platform scans, not a decision input
            "business_risk",     # the injected filter callback, not a capability
        }
        covered = set(DESCRIPTIONS) | {
            "GET_KLINES", "SEARCH_MARKETS", "REFRESH_MARKET",  # supplied by standard_research
        }
        for source in (PredictionMarketApiPlugin, ExecutionGateway):
            for method in dir(source):
                if method.startswith("_") or method in plumbing:
                    continue
                alias = {"get_candles": "GET_KLINES", "search_market_candidates": "SEARCH_MARKETS"}
                tool = alias.get(method, method.upper())
                with self.subTest(capability=f"{source.__name__}.{method}"):
                    self.assertIn(tool, covered, f"{method} has no tool the model can call")

    def test_every_described_tool_actually_dispatches(self) -> None:
        """A described tool with no handler is a promise the model cannot cash."""
        for name in DESCRIPTIONS:
            with self.subTest(tool=name):
                self.assertTrue(hasattr(self.tools, f"_{name.lower()}"), name)

    def test_reads_reach_the_platform(self) -> None:
        self.tools.execute("LIST_TOPICS", {})
        self.tools.execute("GET_TOPIC", {"topic_id": "t1"})
        self.tools.execute("GET_ORDER_BOOK", {"market_id": "m", "outcome_id": "o"})
        self.tools.execute("SYNC_TIME", {})
        self.assertEqual(
            self.platforms["binance"].raw.calls,
            ["list_topics", "get_topic", "get_order_book", "sync_time"],
        )

    def test_a_tool_can_name_another_platform(self) -> None:
        self.tools.execute("GET_ORDER_BOOK", {"market_id": "m", "outcome_id": "o", "platform": "polymarket"})
        self.assertEqual(self.platforms["polymarket"].raw.calls, ["get_order_book"])
        self.assertEqual(self.platforms["binance"].raw.calls, [])

    def test_an_unknown_platform_says_how_to_find_the_right_one(self) -> None:
        with self.assertRaises(KeyError) as caught:
            self.tools.execute("GET_ORDER_BOOK", {"market_id": "m", "outcome_id": "o", "platform": "nope"})
        self.assertIn("LIST_PLATFORMS", str(caught.exception))

    def test_comparing_two_platforms_normalises_both_books(self) -> None:
        result = self.tools.execute("COMPARE_OUTCOMES", {"targets": [
            {"platform": "binance", "market_id": "m", "outcome_id": "o"},
            {"platform": "polymarket", "market_id": "m", "outcome_id": "o"},
        ]})
        self.assertEqual([row["implied_probability"] for row in result["compared"]], [0.41, 0.56])
        self.assertAlmostEqual(result["implied_probability_gap"], 0.15)

    def test_one_unreachable_book_does_not_hide_the_others(self) -> None:
        self.platforms["polymarket"].raw.get_order_book = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
        result = self.tools.execute("COMPARE_OUTCOMES", {"targets": [
            {"platform": "binance", "market_id": "m", "outcome_id": "o"},
            {"platform": "polymarket", "market_id": "m", "outcome_id": "o"},
        ]})
        self.assertIn("implied_probability", result["compared"][0])
        self.assertIn("error", result["compared"][1])

    def test_placing_an_order_quotes_first_then_places(self) -> None:
        result = self.tools.execute("PLACE_ORDER", {
            "side": "BUY", "price": 0.5, "quantity": 4,
            "market_id": "m", "outcome_id": "o", "reason": "test",
        })
        self.assertEqual([call[0] for call in self.platforms["binance"].gateway.calls], ["quote", "order"])
        self.assertEqual(result["order"]["status"], "FILLED")

    def test_the_account_book_is_readable_per_platform(self) -> None:
        snapshot = self.tools.execute("READ_ACCOUNT", {"platform": "polymarket"})
        self.assertEqual(snapshot["platform"], "polymarket")
        self.assertEqual(snapshot["net_result"], 0.0)


class ToolsStillPassFiltersTests(unittest.TestCase):
    """Adding tools must not create a way around the two chains."""

    def setUp(self):
        self.risk = RiskCoordinator()

        class Deny:
            target = "market:*"
            def evaluate(inner, operation, context):
                return RuleDecision("REJECT", f"business risk refuses {operation}")
            def manifest(inner): return {}

        self.risk.register(Deny())
        self.platforms = {"binance": platform("binance", self.risk)}
        self.tools = MarketToolset(self.platforms, "binance")

    def test_business_risk_still_refuses_a_read_made_through_a_tool(self) -> None:
        with self.assertRaises(MarketActionRejected):
            self.tools.execute("GET_ORDER_BOOK", {"market_id": "m", "outcome_id": "o"})
        self.assertEqual(self.platforms["binance"].raw.calls, [])

    def test_business_risk_still_refuses_list_topics_made_through_a_tool(self) -> None:
        with self.assertRaises(MarketActionRejected):
            self.tools.execute("LIST_TOPICS", {})
        self.assertEqual(self.platforms["binance"].raw.calls, [])


if __name__ == "__main__":
    unittest.main()
