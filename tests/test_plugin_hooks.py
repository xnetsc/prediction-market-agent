from ._support import *

class PluginAndHookTests(unittest.TestCase):
    def test_binance_adapter_normalizes_platform_fields(self) -> None:
        topic = BinancePredictionApiPlugin._topic(
            {"marketTopicId": 12, "title": "T", "liquidity": "42.5"}
        )
        market = BinancePredictionApiPlugin._market(
            {
                "marketId": 34,
                "title": "UP",
                "tradingStatus": "OPEN",
                "outcomes": [{"tokenId": 56, "name": "YES", "chance": "0"}],
            }
        )
        self.assertEqual(topic.topic_id, "12")
        self.assertEqual(topic.liquidity_usdt, 42.5)
        self.assertEqual(market.market_id, "34")
        self.assertEqual(market.outcomes[0].displayed_probability, 0.0)

    def test_polymarket_adapter_normalizes_json_encoded_outcomes(self) -> None:
        topic = PolymarketApiPlugin._topic(
            {"id": "event-1", "title": "Event", "active": True, "liquidity": "50"}
        )
        market = PolymarketApiPlugin._market(
            {
                "conditionId": "condition-1",
                "question": "Will it happen?",
                "active": True,
                "acceptingOrders": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.25", "0.75"]',
                "clobTokenIds": '["yes-token", "no-token"]',
            }
        )
        self.assertEqual(topic.status, "OPEN")
        self.assertEqual(market.market_id, "condition-1")
        self.assertEqual(market.outcomes[0].outcome_id, "yes-token")
        self.assertEqual(market.outcomes[0].displayed_probability, 0.25)

    def test_registry_rejects_unregistered_plugin(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown API plugin"):
            ApiPluginRegistry().create(("missing",), config(Path("unused-state.json")))

    def test_strategy_file_has_stable_hash(self) -> None:
        path = Path(__file__).parents[1] / "src/prediction_market_agent/plugins/strategies/prompts/general_agent.md"
        self.assertEqual(
            load_strategy(path).sha256,
            load_strategy(path).sha256,
        )

    def test_order_lifecycle_hooks_are_emitted(self) -> None:
        events: list[str] = []
        hooks = HookManager()
        for name in HookManager.EVENTS:
            hooks.register(name, lambda event, payload: events.append(event))
        gateway = ExecutionGateway(
            (state := AccountState(starting_capital=80, cash=80)),
            account_risk(state),
            platform="fake",
            write_transport=type(
                "Transport",
                (),
                {
                    "get_quote": lambda self, **values: {"quoteId": "q", "averagePrice": values["reference_price"], "expireAt": 4_102_444_800_000},
                    "place_order": lambda self, **values: {"orderId": "o", "status": "FILLED"},
                },
            )(),
            hooks=hooks,
        )
        quote = gateway.get_quote(
            token_id="t",
            side="BUY",
            price=0.5,
            quantity=3,
            fee_bps=0,
            market_topic_id="topic",
            market_id="market",
            symbol="",
            direction="YES",
        )
        gateway.place_order(quote)
        self.assertEqual(
            events,
            ["before_quote", "after_quote", "before_order", "before_fill", "after_fill", "after_order"],
        )
