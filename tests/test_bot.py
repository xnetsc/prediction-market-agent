from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prediction_paper_bot.broker import ExecutionError, ExecutionGateway
from prediction_paper_bot.decision import (
    AgentDecisionProvider,
    Decision,
    DecisionProviderError,
    FallbackDecisionProvider,
    StructuredResult,
)
from prediction_paper_bot.dashboard import AuditData
from prediction_paper_bot.engine import TradingEngine
from prediction_paper_bot.memory import SessionMemory
from prediction_paper_bot.models import AccountState, Config, StateStore
from prediction_paper_bot.hooks import HookManager
from prediction_paper_bot.plugins.binance import BinancePredictionApiPlugin
from prediction_paper_bot.plugins.binance_config import BinancePluginConfig
from prediction_paper_bot.plugins.binance_write import BinancePredictionWriteTransport
from prediction_paper_bot.plugins.polymarket import PolymarketApiPlugin
from prediction_paper_bot.plugins.polymarket_config import PolymarketPluginConfig
from prediction_paper_bot.plugins.registry import ApiPluginRegistry
from prediction_paper_bot.plugins.base import (
    ApiCapabilities,
    Market,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
)
from prediction_paper_bot.risk import (
    NetworkGateError,
    NetworkWriteGate,
    RiskCoordinator,
)
from prediction_paper_bot.risk_plugins.agent_actions import AgentActionRuleEngine
from prediction_paper_bot.risk_plugins.dynamic_python import DynamicPythonRuleEngine
from prediction_paper_bot.risk_plugins.portfolio_limits import (
    AccountLimitEngine,
    PortfolioLimitSettings,
    PortfolioLimitsContribution,
)
from prediction_paper_bot.strategy_plugin import DecisionStrategyPlugin


def config(path: Path) -> Config:
    return Config(state_file=path)


def load_strategy(path: Path) -> DecisionStrategyPlugin:
    return DecisionStrategyPlugin.load(
        path,
        topic_statuses=("OPEN",),
        market_statuses=("OPEN",),
        outcome_names=("YES",),
        minimum_topic_liquidity=0,
    )


BINANCE_ENV = {
    "BINANCE_API_BASE_URL": "https://api.binance.com",
    "BINANCE_PREDICTION_ACCOUNT_TYPE": "SPOT",
    "BINANCE_PREDICTION_SLIPPAGE_BPS": "100",
    "BINANCE_HTTP_PROXY": "DIRECT",
    "BINANCE_NETWORK_RULES_JSON": '{"schemes":["https"],"hosts":["api.binance.com"],"methods":["GET","POST"],"paths_by_method":{"GET":["/*"],"POST":["/*"]}}',
}

POLYMARKET_ENV = {
    "POLYMARKET_GAMMA_URL": "https://gamma-api.polymarket.com",
    "POLYMARKET_CLOB_URL": "https://clob.polymarket.com",
    "POLYMARKET_DATA_URL": "https://data-api.polymarket.com",
    "POLYMARKET_RELAYER_URL": "https://relayer-v2.polymarket.com",
    "POLYMARKET_RPC_URL": "https://polygon.drpc.org",
    "POLYMARKET_CHAIN_ID": "137",
    "POLYMARKET_HTTP_PROXY": "DIRECT",
    "POLYMARKET_NETWORK_RULES_JSON": '{"schemes":["https"],"hosts":["clob.polymarket.com"],"methods":["GET","POST","DELETE"],"paths_by_method":{"GET":["/*"],"POST":["/*"],"DELETE":["/*"]}}',
}


def configured_read_gate() -> NetworkWriteGate:
    return NetworkWriteGate(
        allowed_hosts=frozenset({"api.binance.com"}),
        allowed_schemes=frozenset({"https"}),
        allowed_methods=frozenset({"GET"}),
        allowed_read_paths=frozenset({"/api/v3/time"}),
        target_name="network:test",
    )


def account_risk(state: AccountState, platform: str = "test") -> AccountLimitEngine:
    return AccountLimitEngine(
        platform,
        state,
        PortfolioLimitSettings(
            total_capital=80,
            loss_limit=7,
            max_position=5.25,
            max_exposure=20,
            min_order_notional=1.25,
            allocations={platform: state.starting_capital},
        ),
    )


class NetworkGateTests(unittest.TestCase):
    def test_get_allowlist(self) -> None:
        gate = configured_read_gate()
        gate.check("GET", "https://api.binance.com/api/v3/time")

    def test_blocks_every_network_write(self) -> None:
        gate = configured_read_gate()
        with self.assertRaises(NetworkGateError):
            gate.check("POST", "https://api.binance.com/api/v3/time")

    def test_blocks_unlisted_path_even_with_get(self) -> None:
        gate = configured_read_gate()
        with self.assertRaises(NetworkGateError):
            gate.check(
                "GET",
                "https://api.binance.com/sapi/v1/w3w/wallet/prediction/trade/place-order-bundle",
            )

    def test_network_policy_is_data_driven(self) -> None:
        gate = NetworkWriteGate(
            allowed_hosts=frozenset({"example.test"}),
            allowed_schemes=frozenset({"https"}),
            allowed_methods=frozenset({"POST"}),
            allowed_read_paths=frozenset({"/configured"}),
            target_name="network:test",
        )
        gate.check("POST", "https://example.test/configured")
        with self.assertRaises(NetworkGateError):
            gate.check("GET", "https://example.test/configured")

    def test_method_specific_glob_paths(self) -> None:
        gate = NetworkWriteGate(
            allowed_hosts=frozenset({"example.test"}),
            allowed_schemes=frozenset({"https"}),
            allowed_methods=frozenset({"GET", "POST"}),
            allowed_read_paths=frozenset(),
            allowed_paths_by_method={
                "GET": frozenset({"/events/*"}),
                "POST": frozenset({"/order"}),
            },
        )
        gate.check("GET", "https://example.test/events/123")
        gate.check("POST", "https://example.test/order")
        with self.assertRaises(NetworkGateError):
            gate.check("POST", "https://example.test/events/123")


class ConfigurationTests(unittest.TestCase):
    def test_platform_credentials_are_plugin_private_and_manifests_are_redacted(self) -> None:
        common = Config()
        self.assertFalse(hasattr(common, "api_key"))
        self.assertFalse(hasattr(common, "polymarket_private_key"))
        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "binance-visible-only-to-plugin",
                "BINANCE_API_SECRET": "binance-secret",
                "POLYMARKET_PRIVATE_KEY": "private-key",
                "POLYMARKET_API_KEY": "l2-key",
                "POLYMARKET_API_SECRET": "l2-secret",
                "POLYMARKET_API_PASSPHRASE": "l2-passphrase",
            },
            clear=False,
        ):
            manifests = [
                BinancePluginConfig.from_mapping({**BINANCE_ENV, **os.environ}).manifest(),
                PolymarketPluginConfig.from_mapping({**POLYMARKET_ENV, **os.environ}).manifest(),
            ]
        encoded = str(manifests)
        self.assertNotIn("binance-secret", encoded)
        self.assertNotIn("private-key", encoded)
        self.assertNotIn("l2-secret", encoded)

    def test_from_env_automatically_loads_local_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            dotenv = root / ".env"
            management = root / "management.json"
            management.write_text(
                '{"enabled":{"api":["a"],"decision_provider":["p"],'
                '"research_tool":["r"]},"decision_strategy":"s"}',
                encoding="utf-8",
            )
            dotenv.write_text(
                f"BOT_MANAGEMENT_FILE={management}\nAGENT_MAX_TOOL_STEPS=5\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                loaded = Config.from_env()
            finally:
                os.chdir(previous)
            self.assertEqual(loaded.agent_max_tool_steps, 5)
            self.assertEqual(loaded.loaded_env_file, str(dotenv.resolve()))

    def test_process_environment_has_priority_over_dotenv(self) -> None:
        environment = {"AGENT_MAX_TOOL_STEPS": "7"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ):
            root = Path(directory)
            management = root / "management.json"
            management.write_text(
                '{"enabled":{"api":["a"],"decision_provider":["p"],'
                '"research_tool":["r"]},"decision_strategy":"s"}',
                encoding="utf-8",
            )
            (root / ".env").write_text(
                f"BOT_MANAGEMENT_FILE={management}\nAGENT_MAX_TOOL_STEPS=5\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                loaded = Config.from_env()
            finally:
                os.chdir(previous)
            self.assertEqual(loaded.agent_max_tool_steps, 7)

    def test_financial_limits_are_not_generic_config_fields(self) -> None:
        common = Config()
        self.assertFalse(hasattr(common, "starting_capital"))
        self.assertFalse(hasattr(common, "stop_loss"))
        self.assertFalse(hasattr(common, "platform_allocations_json"))

    def test_platform_provider_and_policy_controls_are_not_generic_config_fields(self) -> None:
        common = Config()
        forbidden = {
            "http_proxy", "execution_mode", "risk_rules_file", "api_key", "api_secret",
            "private_key", "codex_cli_path", "claude_cli_path", "compatible_api_key",
            "agent_allowed_tools", "agent_allowed_trade_actions", "risk_filter_modules",
            "min_liquidity",
        }
        self.assertFalse(forbidden & set(common.__dict__))

    def test_all_research_tools_may_be_disabled(self) -> None:
        common = Config(
            decision_providers=("provider",),
            market_api_plugins=("api",),
            research_tool_plugins=(),
            decision_strategy_name="strategy",
        )
        common.validate()


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

            def outcome_won(self, detail, market, outcome):
                del detail, market, outcome
                return None

        class BuyProvider:
            name = "buy-test"
            available_names = ("buy-test",)
            unavailable = {}

            def decide(self, context, *, tool_executor=None, step_recorder=None, tool_descriptions=None):
                del context, tool_executor, step_recorder, tool_descriptions
                from prediction_paper_bot.decision import ProviderResult

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
                max_topics_per_cycle=1,
                max_decisions_per_cycle=1,
                risk_plugins=("test_portfolio", "test_action"),
                research_tool_plugins=(),
                decision_strategy_name="general_agent",
            )
            contribution = PortfolioLimitsContribution(
                PortfolioLimitSettings(80, 7, 5.25, 20, 1.25, {"fake": 80})
            )
            strategy_path = (
                Path(__file__).parents[1]
                / "src/prediction_paper_bot/strategies/general_agent.md"
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
            with patch("prediction_paper_bot.engine.load_plugin_catalog", return_value=catalog), patch(
                "prediction_paper_bot.engine.load_api_plugins", return_value=(registry, [FakePlugin()])
            ), patch("prediction_paper_bot.engine.make_provider", return_value=BuyProvider()):
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


class DecisionAndMemoryTests(unittest.TestCase):
    def test_decision_validation(self) -> None:
        decision = Decision.from_mapping(
            {
                "action": "HOLD",
                "order_type": "MARKET",
                "notional_usdt": 0,
                "quantity_fraction": 0,
                "limit_price": None,
                "confidence": 0.7,
                "estimated_probability": 0.5,
                "rationale": "No edge",
            }
        )
        self.assertEqual(decision.action, "HOLD")
        with self.assertRaises(DecisionProviderError):
            Decision.from_mapping(
                {
                    **decision.to_dict(),
                    "action": "BUY",
                    "order_type": "LIMIT",
                    "limit_price": None,
                }
            )

    def test_sqlite_memory_saves_full_turn_and_recalls_bounded_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = SessionMemory(Path(directory) / "sessions.sqlite3")
            payload = {
                "market": {"title": "BTC up?"},
                "portfolio": {"equity": 100},
                "large": "x" * 2000,
            }
            memory.record_turn(
                provider="codex",
                market_topic_id=1,
                token_id="token-up",
                input_payload=payload,
                raw_output="complete raw output",
                decision={"action": "HOLD", "rationale": "wait"},
                status="OK",
                platform="fake",
            )
            memory.record_action(
                market_topic_id=1,
                token_id="token-up",
                action="HOLD",
                request={"action": "HOLD"},
                result={"status": "NO_ACTION"},
                platform="fake",
            )
            recalled = memory.recalled_context(
                market_topic_id=1,
                token_id="token-up",
                history_limit=12,
                char_budget=1000,
                platform="fake",
            )
            memory.record_agent_step(
                provider="codex",
                market_topic_id=1,
                token_id="token-up",
                step_index=0,
                input_payload={"prompt": "research"},
                raw_output='{"next_action":"DECIDE"}',
                control={"next_action": "DECIDE"},
                status="DECIDE",
                platform="fake",
            )
            self.assertEqual(
                memory.stats(),
                {"saved_turns": 1, "saved_actions": 1, "saved_agent_steps": 1, "saved_decisions": 0},
            )
            self.assertEqual(recalled["prior_turns"][0]["decision"]["action"], "HOLD")
            memory.close()

    def test_decision_ledger_links_context_research_risk_execution_and_ui_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(
                state_file=root / "state.json",
                session_db=root / "sessions.sqlite3",
                market_api_plugins=("binance",),
            )
            memory = SessionMemory(cfg.session_db)
            decision_id = memory.begin_decision(
                platform="binance",
                market_topic_id="topic",
                market_id="market",
                token_id="token",
                strategy_name="test_strategy",
                strategy_sha256="abc",
                context={
                    "market": {"title": "Explainable market"},
                    "order_book": {"best_bid": 0.4, "best_ask": 0.6},
                    "portfolio": {"equity": 12},
                },
            )
            memory.record_agent_step(
                provider="codex", market_topic_id="topic", token_id="token",
                step_index=0, input_payload={"prompt": "why"}, raw_output="raw",
                control={"next_action": "DECIDE"}, status="DECIDE",
                platform="binance", decision_id=decision_id,
            )
            memory.record_turn(
                provider="codex", market_topic_id="topic", token_id="token",
                input_payload={"market": {"title": "Explainable market"}},
                raw_output="raw", decision={"action": "HOLD"}, status="OK",
                platform="binance", decision_id=decision_id,
            )
            memory.record_action(
                market_topic_id="topic", token_id="token", action="HOLD",
                request={"action": "HOLD"}, result={"status": "NO_ACTION"},
                platform="binance", decision_id=decision_id,
            )
            memory.complete_decision(
                decision_id, provider="codex",
                research=[{"tool": "SEARCH_WEB", "result": {"source": "example"}}],
                model_raw_output="raw", proposed_decision={"action": "HOLD"},
                risk_decision={"outcome": "ALLOW"}, final_decision={"action": "HOLD"},
                execution={"status": "NO_ACTION"}, status="NO_ACTION",
            )
            memory.close()
            data = AuditData(cfg)
            try:
                row = data.decisions(10, 0, "binance", "codex", "NO_ACTION", "HOLD")["items"][0]
                self.assertEqual(row["id"], decision_id)
                self.assertEqual(row["agent_steps"], 1)
                self.assertEqual(row["research"][0]["tool"], "SEARCH_WEB")
                self.assertEqual(row["risk_decision"]["outcome"], "ALLOW")
                self.assertEqual(row["execution"]["status"], "NO_ACTION")
            finally:
                data.management.shutdown()

    def test_strategy_plugin_owns_discovery_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.md"
            path.write_text("Configured strategy", encoding="utf-8")
            strategy = DecisionStrategyPlugin.load(
                path,
                topic_statuses=("OPEN",),
                market_statuses=("OPEN",),
                outcome_names=("NO",),
                minimum_topic_liquidity=50,
            )
            topics = [
                Topic("1", "a", "a", "", "", "OPEN", 40, 0),
                Topic("2", "b", "b", "", "", "OPEN", 60, 0),
            ]
            self.assertEqual([item.topic_id for item in strategy.select_topics(topics)], ["2"])
            outcomes = (Outcome("yes", "YES"), Outcome("no", "NO"))
            self.assertEqual([item.name for item in strategy.select_outcomes(outcomes)], ["NO"])

    def test_http_audit_data_exposes_full_saved_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "state.json")
            cfg = Config(
                **{
                    **cfg.__dict__,
                    "session_db": root / "sessions.sqlite3",
                    "market_api_plugins": ("binance",),
                }
            )
            memory = SessionMemory(cfg.session_db)
            memory.record_action(
                platform="binance",
                market_topic_id="topic",
                token_id="token",
                action="BUY_FAILED",
                request={"notional": 2},
                result={"status": "EXECUTION_ERROR", "error": "server rejected"},
            )
            memory.close()
            records = AuditData(cfg).records("actions", 10, 0, "binance")
            self.assertEqual(records["items"][0]["action"], "BUY_FAILED")
            self.assertEqual(records["items"][0]["result"]["error"], "server rejected")


class FakeBackend:
    def __init__(self, name: str, responses: list[dict] | None = None, error: str = ""):
        self.name = name
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[str] = []

    def complete(self, prompt, schema, schema_name):
        self.calls.append(schema_name)
        if self.error:
            raise DecisionProviderError(self.error, "backend raw error")
        value = self.responses.pop(0)
        return StructuredResult(value=value, raw_output=str(value))


def hold_decision() -> dict:
    return {
        "action": "HOLD",
        "order_type": "MARKET",
        "notional_usdt": 0,
        "quantity_fraction": 0,
        "limit_price": None,
        "confidence": 0.7,
        "estimated_probability": 0.5,
        "rationale": "Evidence does not demonstrate a sufficient edge.",
    }


class AgentLoopTests(unittest.TestCase):
    def test_agent_executes_tool_then_decides_and_records_every_step(self) -> None:
        backend = FakeBackend(
            "codex",
            [
                {
                    "next_action": "SEARCH_WEB",
                    "arguments_json": '{"query":"current event"}',
                    "reason": "Need current evidence",
                },
                {
                    "next_action": "DECIDE",
                    "arguments_json": "{}",
                    "reason": "Enough evidence",
                },
                hold_decision(),
            ],
        )
        cfg = config(Path("unused-state.json"))
        provider = AgentDecisionProvider(backend, cfg)
        tool_calls: list[tuple[str, dict]] = []
        records: list[dict] = []

        def tool(name, arguments):
            tool_calls.append((name, arguments))
            return {"results": [{"title": "Evidence"}]}

        result = provider.decide(
            {"market": {"title": "Test market"}},
            tool_executor=tool,
            step_recorder=lambda **values: records.append(values),
        )

        self.assertEqual(tool_calls, [("SEARCH_WEB", {"query": "current event"})])
        self.assertEqual(backend.calls, ["agent_control", "agent_control", "trade_decision"])
        self.assertEqual(result.provider, "codex")
        self.assertEqual(result.decision.action, "HOLD")
        self.assertEqual(len(result.research_trace), 1)
        self.assertEqual([item["status"] for item in records], ["TOOL_OK", "DECIDE", "OK"])
        self.assertEqual([item["step_index"] for item in records], [0, 1, 2])

    def test_provider_priority_falls_back_after_runtime_failure(self) -> None:
        cfg = config(Path("unused-state.json"))
        first = AgentDecisionProvider(FakeBackend("codex", error="unavailable"), cfg)
        second = AgentDecisionProvider(
            FakeBackend(
                "claude",
                [
                    {
                        "next_action": "DECIDE",
                        "arguments_json": "{}",
                        "reason": "No research needed",
                    },
                    hold_decision(),
                ],
            ),
            cfg,
        )
        fallback = FallbackDecisionProvider(
            [first, second], unavailable={}, configured_names=("codex", "claude")
        )
        result = fallback.decide({"market": {}}, tool_executor=lambda *_: {})
        self.assertEqual(result.provider, "claude")

    def test_strategy_text_is_injected_into_control_and_final_prompts(self) -> None:
        backend = FakeBackend(
            "codex",
            [
                {"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"},
                hold_decision(),
            ],
        )
        prompts: list[str] = []
        original = backend.complete

        def capture(prompt, schema, schema_name):
            prompts.append(prompt)
            return original(prompt, schema, schema_name)

        backend.complete = capture
        provider = AgentDecisionProvider(backend, config(Path("unused-state.json")))
        provider.decide(
            {
                "market": {},
                "decision_strategy_plugin": {
                    "path": "/tmp/test.md",
                    "sha256": "hash",
                    "instructions": "UNIQUE_STRATEGY_SENTINEL",
                },
            },
            tool_executor=lambda *_: {},
        )
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("UNIQUE_STRATEGY_SENTINEL" in prompt for prompt in prompts))


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
        path = Path(__file__).parents[1] / "src/prediction_paper_bot/strategies/general_agent.md"
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


if __name__ == "__main__":
    unittest.main()
