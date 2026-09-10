from ._support import *

class RiskAndExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.cfg = config(self.path)
        self.state = AccountState(starting_capital=80, cash=80)
        settings = PortfolioLimitSettings(80, 7, 5.25, 20, 1.25, {"test": 80})
        contribution = PortfolioLimitsContribution(settings)
        risk = contribution.create_account_engine("test", self.state)
        contribution.create_global_engine({"test": self.state})
        class RecordingTransport:
            def __init__(self):
                self.calls = []
                self.counter = 0

            def get_quote(inner, **values):
                inner.calls.append(("quote", values))
                inner.counter += 1
                return {"quoteId": f"q-{inner.counter}", "averagePrice": values["reference_price"], "expireAt": 4_102_444_800_000}

            def place_order(inner, **values):
                inner.calls.append(("order", values))
                return {"orderId": f"o-{inner.counter}", "status": "OPEN" if values["order_type"] == "LIMIT" else "FILLED"}

            def cancel_orders(inner, order_ids):
                inner.calls.append(("cancel", order_ids))
                return {"canceled": list(order_ids), "failed": []}

            def redeem(inner, outcome_ids):
                inner.calls.append(("redeem", outcome_ids))
                return {"status": "COMPLETED", "outcomeIds": list(outcome_ids)}

            def transfer(inner, direction, amount):
                inner.calls.append(("transfer", {"direction": direction, "amount": amount}))
                return {"status": "COMPLETED", "direction": direction}

        self.transport = RecordingTransport()
        self.gateway = ExecutionGateway(
            self.state, risk, platform="fake", write_transport=self.transport
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def quote(self, side: str, price: float, quantity: float, order_type: str = "MARKET"):
        return self.gateway.get_quote(
            token_id="token-up",
            side=side,
            price=price,
            quantity=quantity,
            fee_bps=200,
            market_topic_id=1,
            market_id=2,
            symbol="BTCUSDT",
            direction="UP",
            order_type=order_type,
        )

    def test_market_buy_calls_transport_and_respects_position_limit(self) -> None:
        order = self.gateway.place_order(self.quote("BUY", 0.5, 10), reason="test")
        self.assertEqual(order.status, "FILLED")
        self.assertAlmostEqual(self.state.cash, 74.9)
        self.assertEqual([item[0] for item in self.transport.calls[:2]], ["quote", "order"])
        with self.assertRaises(ExecutionError):
            self.gateway.place_order(self.quote("BUY", 0.5, 1))

    def test_risk_plugin_net_result_stop_uses_profit_loss_difference(self) -> None:
        self.gateway.place_order(self.quote("BUY", 0.5, 10))
        self.gateway.mark("token-up", 0.0)
        self.assertFalse(self.state.halted)
        self.state.cash = 65.0
        self.gateway.mark("token-up", 0.0)
        self.assertTrue(self.state.halted)
        self.assertLessEqual(self.state.risk_metrics["net_result"], -7)

    def test_limit_order_and_cancel_call_transport(self) -> None:
        order = self.gateway.place_order(self.quote("BUY", 0.4, 5, "LIMIT"))
        self.assertEqual(order.status, "OPEN")
        result = self.gateway.cancel_orders([order.order_id])
        self.assertEqual(result["canceled"], [order.order_id])
        self.assertEqual(order.status, "CANCELED")

    def test_open_limit_orders_reserve_plugin_budget(self) -> None:
        first = self.gateway.place_order(self.quote("BUY", 0.5, 10, "LIMIT"))
        self.assertEqual(first.status, "OPEN")
        with self.assertRaises(ExecutionError):
            self.gateway.place_order(self.quote("BUY", 0.5, 1, "LIMIT"))

    def test_market_fill_preserves_metadata(self) -> None:
        self.gateway.place_order(self.quote("BUY", 0.55, 5, "MARKET"))
        self.assertEqual(self.state.positions["token-up"].market_topic_id, 1)
        self.assertEqual(self.state.positions["token-up"].symbol, "BTCUSDT")

    def test_outbound_transfer_preserves_plugin_net_result(self) -> None:
        before = self.state.risk_metrics["net_result"]
        result = self.gateway.transfer("OUTBOUND", 8)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertAlmostEqual(self.state.risk_metrics["net_result"], before)

    def test_state_round_trip(self) -> None:
        self.gateway.place_order(self.quote("BUY", 0.5, 10))
        store = StateStore(self.path, 80)
        store.save(self.state)
        loaded = store.load()
        self.assertAlmostEqual(loaded.equity, self.state.equity)
        self.assertIn("token-up", loaded.positions)

    def test_dynamic_python_rule_can_only_reduce(self) -> None:
        module_path = Path(self.temp.name) / "risk_rule.py"
        module_path.write_text(
            "def evaluate(operation, context):\n"
            "    return {'outcome': 'ADJUST', 'reason': 'cap', 'adjusted_value': 2.5}\n",
            encoding="utf-8",
        )
        rule = DynamicPythonRuleEngine(module_path, 0)
        result = rule.evaluate("BUY", {"requested_value": 5})
        self.assertEqual(result.outcome, "ADJUST")
        self.assertEqual(result.adjusted_value, 2.5)
        self.assertEqual(rule.manifest()["trust_boundary"], "TRUSTED_IN_PROCESS_PYTHON")

    def test_online_transport_failure_is_saved_as_execution_action(self) -> None:
        topic = Topic("topic", "Title", "Question", "", "test", "OPEN", 1000, 1000)
        outcome = Outcome("token", "YES", 0.5)
        market = Market("market", "YES", "Question", "OPEN", 1000, 1000, (outcome,))

        class FailingTransport:
            def get_quote(self, **values):
                del values
                raise RuntimeError("server rejected write")

        class FakePlugin:
            name = "fake"
            capabilities = ApiCapabilities(
                realtime_order_book=True,
                candles=False,
                market_search=True,
                settlement_status=False,
                supported_order_types=("MARKET",),
                write_workflows=("BUY",),
                data_features=(),
            )
            network_rule_engine = NetworkWriteGate(
                frozenset({"example.test"}),
                frozenset({"https"}),
                frozenset({"GET", "POST"}),
                frozenset({"/read", "/write"}),
                target_name="network:fake",
            )

            def sync_time(self):
                return None

            def list_topics(self, *, offset, limit):
                del offset, limit
                return TopicPage((topic,), False, 1)

            def get_topic(self, topic_id):
                del topic_id
                return TopicDetail(topic, 0, 4_000_000_000_000, 0, (market,))

            def get_order_book(self, market_id, outcome_id):
                del market_id, outcome_id
                return OrderBook((PriceLevel(0.49, 100),), (PriceLevel(0.5, 100),))

            def get_candles(self, reference_symbol, interval="1m", limit=120):
                del reference_symbol, interval, limit
                return []

            def create_write_gateway(self, state, risk):
                return ExecutionGateway(
                    state, risk, platform=self.name, write_transport=FailingTransport()
                )

            def search_market_candidates(self, query, limit):
                del query, limit
                return []

            def write_transport(self):
                return FailingTransport()

            def configuration_manifest(self):
                return {}

            def cycle_limits(self):
                return (1, 1)

            def topic_page_size(self):
                return 1

            def outcome_won(self, detail, market, outcome):
                del detail, market, outcome
                return None

        class BuyProvider:
            name = "buy-test"
            available_names = ("buy-test",)
            unavailable = {}

            def decide(self, context, *, tool_executor=None, step_recorder=None, tool_descriptions=None, instructions=None):
                del context, tool_executor, step_recorder, tool_descriptions, instructions
                from prediction_market_agent.agent.decision import ProviderResult

                return ProviderResult(
                    Decision("BUY", "MARKET", 2, 0, None, 1, 0.8, "test failure audit"),
                    '{"action":"BUY"}',
                    self.name,
                    [],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(
                state_file=root / "state.json",
                session_db=root / "session.sqlite3",
                market_api_plugins=("fake",),
                risk_plugins=("test_portfolio", "test_action"),
                research_tool_plugins=(),
                decision_strategy_name="general_agent",
            )
            contribution = PortfolioLimitsContribution(
                PortfolioLimitSettings(80, 7, 5.25, 20, 1.25, {"fake": 80})
            )
            strategy_path = (
                Path(__file__).parents[1]
                / "src/prediction_market_agent/plugins/strategies/prompts/general_agent.md"
            )

            class Catalog:
                def get(self, kind, name):
                    if kind == "decision_strategy":
                        return type(
                            "Spec",
                            (),
                            {"factory": lambda self, config: load_strategy(strategy_path)},
                        )()
                    return type(
                        "Spec",
                        (),
                        {
                            "factory": lambda self, config, services: (
                                contribution
                                if name == "test_portfolio"
                                else AgentActionRuleEngine(
                                    ("SEARCH_WEB",),
                                    ("BUY", "SELL", "HOLD", "CANCEL"),
                                )
                            )
                        },
                    )()

            catalog = Catalog()
            registry = ApiPluginRegistry()
            with patch("prediction_market_agent.runtime.bootstrap.load_plugin_catalog", return_value=catalog), patch(
                "prediction_market_agent.runtime.bootstrap.load_api_plugins", return_value=(registry, [FakePlugin()])
            ), patch("prediction_market_agent.runtime.bootstrap.make_provider", return_value=BuyProvider()):
                engine = TradingEngine(cfg)
                engine.run_once()
            connection = __import__("sqlite3").connect(cfg.session_db)
            action, result_json = connection.execute(
                "SELECT action,result_json FROM execution_actions"
            ).fetchone()
            connection.close()
            self.assertEqual(action, "BUY_FAILED")
            self.assertIn("server rejected write", result_json)
            self.assertNotIn('"simulated"', result_json)

    def test_generic_gateway_requires_plugin_normalized_order_status(self) -> None:
        quote = self.quote("BUY", 0.5, 4)
        self.transport.place_order = lambda **values: {
            "orderId": "vendor-order",
            "status": "vendor-specific",
        }
        with self.assertRaisesRegex(ExecutionError, "normalized order status"):
            self.gateway.place_order(quote)

    def test_no_enabled_rule_for_target_adds_no_policy(self) -> None:
        decision = RiskCoordinator().evaluate(
            "agent:actions", "BUY", {"requested_value": 3}
        )
        self.assertEqual(decision.outcome, "ALLOW")

    def test_binance_transport_normalizes_platform_order_status(self) -> None:
        settings = BinancePluginConfig.from_mapping(BINANCE_ENV)
        transport = BinancePredictionWriteTransport(settings, configured_read_gate())
        with patch.object(
            transport,
            "_post",
            return_value={"orderId": "vendor-order", "orderStatus": "LIVE"},
        ):
            result = transport.place_order(
                quote_id="quote", order_type="LIMIT", price_limit="0.5"
            )
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["platformStatus"], "LIVE")
