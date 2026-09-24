from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from prediction_market_agent.agent.decision_evaluator import screening_feedback
from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery
from prediction_market_agent.agent.market_playbook import SECTIONS, read_market_playbook
from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy
from prediction_market_agent.plugin_system.contracts import Market, OrderBook, Outcome, PriceLevel, TopicDetail
from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin
from prediction_market_agent.runtime.evaluation import compact_market, near_touch_levels
from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox
from prediction_market_agent.runtime.market_tools import MarketToolset
from prediction_market_agent.runtime.memory import SessionMemory

from tests.test_market_discovery import FakePlugin


class MarketSelectionRulesTests(unittest.TestCase):
    def test_nearest_depth_is_sorted_from_the_touch_not_venue_array_order(self) -> None:
        book = OrderBook(
            (PriceLevel(.01, 900), PriceLevel(.38, 4), PriceLevel(.39, 2)),
            (PriceLevel(.99, 800), PriceLevel(.42, 5), PriceLevel(.41, 3)),
        )
        levels = near_touch_levels(book, 2)
        self.assertEqual([item["price"] for item in levels["top_bids"]], [.39, .38])
        self.assertEqual([item["price"] for item in levels["top_asks"]], [.41, .42])
        self.assertEqual(levels["top_asks"][0]["quantity"], 3)

    def test_one_sample_cannot_condemn_all_contracts_in_event(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            memory = SessionMemory(Path(root) / "round.sqlite3")
            plugin = FakePlugin(1)
            topic = plugin.topics[0]
            now = int(time.time() * 1000)
            markets = (
                Market("fringe", "Exact score", "Exact score 7-7?", "OPEN", 100, 1,
                       (Outcome("fringe-yes", "Yes", .02),), now + 120_000),
                Market("winner", "Match winner", "Will the home team win?", "OPEN", 90, 4,
                       (Outcome("winner-yes", "Yes", .52),), now + 3_600_000),
            )
            plugin.get_topic = lambda _id: TopicDetail(topic, 0, now + 86_400_000, 0, markets)
            queried: list[str] = []

            def book(market_id: str, _outcome_id: str) -> OrderBook:
                queried.append(market_id)
                return OrderBook((PriceLevel(.01, 2),), ())

            plugin.get_order_book = book
            toolbox = _DiscoveryToolbox(
                plugin=plugin, memory=memory, platform="fake",
                budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
            )
            entry = toolbox.verify([topic.topic_id])[topic.topic_id]
            self.assertEqual(queried, ["fringe"])
            self.assertEqual(entry["sampled_market_count"], 1)
            self.assertEqual(entry["unverified_open_markets"], 1)
            self.assertEqual(len(entry["market_options"]), 2)
            self.assertEqual(entry["book_one_sided"], "nobody is selling")
            self.assertEqual(entry["seconds_remaining"], 120)
            self.assertGreater(entry["event_seconds_remaining"], entry["seconds_remaining"])
            self.assertEqual(entry["best_bid_size"], 2)

    def test_market_fee_and_deadline_are_not_replaced_by_event_aggregate(self) -> None:
        plugin = FakePlugin(1)
        topic = plugin.topics[0]
        market = Market(
            "m", "M", "Q", "OPEN", 15, 40, (Outcome("y", "Yes", .5),),
            1234, True, {"rate": .04, "exponent": 1},
        )
        detail = TopicDetail(topic, 0, 9999, 0, (market,))
        context = compact_market("fake", topic, detail, market)
        self.assertEqual(context["end_date_ms"], 1234)
        self.assertIsNone(context["fee_rate_bps"])
        self.assertEqual(context["fee_schedule"]["rate"], .04)

    def test_polymarket_adapter_preserves_per_market_fee_terms(self) -> None:
        market = PolymarketApiPlugin._market({
            "id": "m", "question": "Will it happen?", "active": True,
            "acceptingOrders": True, "closed": False,
            "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["yes", "no"]', "endDate": "2026-10-01T12:00:00Z",
            "feesEnabled": True,
            "feeSchedule": {"rate": .04, "exponent": 1, "takerOnly": True},
        })
        self.assertTrue(market.fees_enabled)
        self.assertEqual(market.fee_schedule["rate"], .04)
        self.assertIsNotNone(market.end_time_ms)

    def test_playbook_is_loaded_one_section_at_a_time(self) -> None:
        tool = MarketToolset({}, "fake")
        answer = tool.execute("READ_MARKET_PLAYBOOK", {"section": "execution"})
        self.assertTrue(answer["ok"])
        self.assertIn("ONE full current spread", answer["guide"])
        self.assertEqual(len(SECTIONS), 6)
        self.assertNotIn("guide", tool.execute("READ_MARKET_PLAYBOOK", {"section": "all"}))
        self.assertIn("READ_MARKET_PLAYBOOK", BuiltInMarketDiscovery().instructions)
        self.assertIn("READ_MARKET_PLAYBOOK", BuiltInDecisionStrategy().instructions)
        self.assertNotIn("pays the spread twice", BuiltInDecisionStrategy().instructions)

    def test_feedback_requires_samples_and_does_not_treat_hold_as_loss(self) -> None:
        small = screening_feedback({"by_screening_action": {"PRIORITIZE": {"HOLD": 2}}})
        self.assertEqual(len(small["guidance"]), 1)
        large = screening_feedback({"by_screening_action": {
            "PRIORITIZE": {"HOLD": 19, "BUY": 1},
            "DEFER": {"HOLD": 9, "BUY": 1},
        }})
        self.assertEqual(len(large["guidance"]), 3)
        self.assertIn("not P&L", large["basis"])
        self.assertEqual(large["reviewed_counts"]["PRIORITIZE"], 20)


if __name__ == "__main__":
    unittest.main()
