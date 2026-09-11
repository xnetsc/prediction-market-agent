from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from prediction_market_agent.core.config import Config
from prediction_market_agent.agent.decision import Decision, ProviderResult
from prediction_market_agent.runtime.engine import TradingEngine
from prediction_market_agent.plugin_system.contracts import PredictionMarketApiPlugin
from prediction_market_agent.plugin_system.registry import load_api_plugins


class ProductionApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Config.load()
        cls.temp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _config(self, names: tuple[str, ...]) -> Config:
        root = Path(self.temp.name)
        return replace(
            self.base,
            market_api_plugins=names,
            state_file=root / ("-".join(names) + ".json"),
            session_db=root / ("-".join(names) + ".sqlite3"),
        )

    def _exercise_public_surface(self, plugin: PredictionMarketApiPlugin) -> dict[str, object]:
        plugin.sync_time()
        page = plugin.list_topics(offset=0, limit=5)
        self.assertTrue(page.topics, f"{plugin.name} returned no active topics")
        last_error: Exception | None = None
        for topic in page.topics:
            try:
                detail = plugin.get_topic(topic.topic_id)
                market = next(item for item in detail.markets if item.status == "OPEN")
                outcome = next(item for item in market.outcomes if item.outcome_id)
                book = plugin.get_order_book(market.market_id, outcome.outcome_id)
                self.assertTrue(book.bids or book.asks)
                return {
                    "topic": topic,
                    "detail": detail,
                    "market": market,
                    "outcome": outcome,
                    "book": book,
                }
            except (RuntimeError, StopIteration, ValueError) as error:
                last_error = error
        self.fail(f"{plugin.name} had no usable market/book in sample: {last_error}")

    def test_01_binance_plugin_production_reads_and_configured_write_route(self) -> None:
        config = self._config(("binance",))
        registry, plugins = load_api_plugins(config)
        self.assertIn("binance", registry.registered_names)
        self._exercise_public_surface(plugins[0])

    def test_02_polymarket_plugin_production_reads_and_configured_write_route(self) -> None:
        config = self._config(("polymarket",))
        registry, plugins = load_api_plugins(config)
        self.assertIn("polymarket", registry.registered_names)
        self._exercise_public_surface(plugins[0])

    def test_03_binance_and_polymarket_run_in_one_registry(self) -> None:
        config = self._config(("binance", "polymarket"))
        registry, plugins = load_api_plugins(config)
        self.assertEqual(tuple(item.name for item in plugins), ("binance", "polymarket"))
        self.assertEqual(set(registry.registered_names), {"binance", "polymarket"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self._exercise_public_surface, plugins))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["detail"] is not None for result in results))
        capability_input = {item.name: item.capabilities.to_dict() for item in plugins}
        self.assertEqual(set(capability_input), {"binance", "polymarket"})
        self.assertNotEqual(
            capability_input["binance"]["write_workflows"],
            capability_input["polymarket"]["write_workflows"],
        )

        class HoldProvider:
            name = "integration-hold"
            available_names = ("integration-hold",)
            unavailable = {}

            def decide(self, context, *, tool_executor=None, step_recorder=None, tool_descriptions=None, instructions=None):
                del context, tool_executor, step_recorder, tool_descriptions
                return ProviderResult(
                    decision=Decision(
                        action="HOLD",
                        order_type="MARKET",
                        notional_usdt=0,
                        quantity_fraction=0,
                        limit_price=None,
                        confidence=1,
                        estimated_probability=0.5,
                        rationale="Integration test exercises both online read adapters without writes.",
                    ),
                    raw_output='{"action":"HOLD"}',
                    provider=self.name,
                    research_trace=[],
                )

        engine = TradingEngine(config)
        engine.provider = HoldProvider()
        # Discovery itself needs an Agent; this test exercises the read and execution path,
        # so it stands in for the discovery Agent with the platform's first two topics.
        engine.discovery.discover = lambda *, platform, plugin, maximum_topics: tuple(
            plugin.list_topics(offset=0, limit=2).topics
        )
        status = engine.run_once()
        self.assertEqual(set(status["platforms"]), {"binance", "polymarket"})
        self.assertEqual(
            status["aggregate_equity"],
            sum(item.state.starting_capital for item in engine.platforms.values()),
        )

    def test_04_all_configured_write_routes_reach_servers_and_are_rejected(self) -> None:
        _, plugins = load_api_plugins(self._config(("binance", "polymarket")))
        by_name = {plugin.name: plugin for plugin in plugins}
        binance = by_name["binance"].write_transport()

        calls = {
            "quote": lambda: binance.get_quote(
                outcome_id="integration-invalid-outcome",
                side="BUY",
                amount="0.000000000000000001",
                order_type="MARKET",
                price_limit=None,
                reference_price=0.5,
                fee_bps=0,
            ),
            "order": lambda: binance.place_order(
                quote_id="integration-invalid-quote",
                order_type="MARKET",
                price_limit=None,
            ),
            "cancel": lambda: binance.cancel_orders(["integration-invalid-order"]),
            "redeem": lambda: binance.redeem(["integration-invalid-outcome"]),
            "transfer_in": lambda: binance.transfer("INBOUND", "0.000000000000000001"),
            "transfer_out": lambda: binance.transfer("OUTBOUND", "0.000000000000000001"),
        }
        for name, call in calls.items():
            with self.subTest(platform="binance", workflow=name):
                with self.assertRaisesRegex(RuntimeError, r"Binance write HTTP \d{3}"):
                    call()

        polymarket = by_name["polymarket"]
        transport = polymarket.write_transport()
        probes = (
            (polymarket.settings.clob_url, "POST", "/order"),
            (polymarket.settings.clob_url, "DELETE", "/order"),
            (polymarket.settings.relayer_url, "POST", "/submit"),
        )
        for base_url, method, path in probes:
            with self.subTest(platform="polymarket", method=method, path=path):
                with transport._http_client(base_url) as client:
                    response = client.request(method, path, json={})
                self.assertGreaterEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
