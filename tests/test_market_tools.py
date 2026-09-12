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


class FundingTests(unittest.TestCase):
    """An account with no money cannot trade, so where the money comes from must be explicit."""

    def test_a_platform_that_cannot_read_its_wallet_says_the_figure_is_declared(self) -> None:
        """Labelling a configured number as the platform's own would invite trading money that is
        sitting in a different account."""
        from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
        from tests._support import BINANCE_ENV

        plugin = BinancePredictionApiPlugin({**BINANCE_ENV, "BINANCE_TRADING_CAPITAL": "250"})
        funds = plugin.account_funds()
        self.assertEqual(funds.available, 250.0)
        self.assertEqual(funds.source, "declared")
        self.assertIn("prediction wallet", funds.detail["why_declared"])

    def test_spot_money_is_reported_as_fundable_not_as_spendable(self) -> None:
        """Predictions trade out of a separate wallet; spot is only what a top-up could draw on."""
        from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
        from tests._support import BINANCE_ENV

        plugin = BinancePredictionApiPlugin({**BINANCE_ENV, "BINANCE_TRADING_CAPITAL": "10"})
        plugin.client.spot_balances = lambda: {"USDT": {"free": 900.0, "locked": 0.0}}
        funds = plugin.account_funds()
        self.assertEqual(funds.available, 10.0, "spot money is not spendable on predictions")
        self.assertEqual(funds.detail["fundable_from_spot"], 900.0)

    def test_reported_funds_reach_the_account_the_bot_trades_with(self) -> None:
        """This is the wiring that was missing: reported funds that never became spendable cash."""
        import tempfile, pathlib
        from prediction_market_agent.core.state import StateStore

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(pathlib.Path(directory) / "state.json", 250.0)
            state = store.load()
            self.assertEqual(state.cash, 250.0)
            self.assertEqual(state.starting_capital, 250.0)

    def test_an_undeclared_balance_leaves_the_account_empty_rather_than_guessing(self) -> None:
        import tempfile, pathlib
        from prediction_market_agent.core.state import StateStore

        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(pathlib.Path(directory) / "state.json", 0.0).load()
            self.assertEqual(state.cash, 0.0)

    def test_reported_funds_cap_nothing(self) -> None:
        """It is the ledger's starting point, not a limit; only a filter plugin may refuse."""
        risk = RiskCoordinator()
        runtime = platform("binance", risk)
        runtime.state.cash = 10.0
        runtime.state.starting_capital = 10.0
        tools = MarketToolset({"binance": runtime}, "binance")
        result = tools.execute("PLACE_ORDER", {
            "side": "BUY", "price": 0.5, "quantity": 1000,  # far beyond the opening balance
            "market_id": "m", "outcome_id": "o",
        })
        self.assertEqual(result["order"]["status"], "FILLED")


class FundsSemanticsTests(unittest.TestCase):
    """available means spendable here and now; ensure_funds states a target, not a transfer size."""

    def _binance(self, capital="10", spot=900.0):
        from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
        from tests._support import BINANCE_ENV

        plugin = BinancePredictionApiPlugin({**BINANCE_ENV, "BINANCE_TRADING_CAPITAL": capital})
        plugin.client.spot_balances = lambda: {"USDT": {"free": spot, "locked": 0.0}}
        return plugin

    def test_money_that_would_need_moving_is_not_counted_as_available(self) -> None:
        plugin = self._binance(capital="10", spot=900.0)
        funds = plugin.account_funds()
        self.assertEqual(funds.available, 10.0)
        self.assertNotEqual(funds.available, 900.0, "spot money is not spendable on predictions")
        self.assertEqual(funds.detail["fundable_from_spot"], 900.0)

    def test_ensure_funds_asks_for_a_target_not_a_transfer_amount(self) -> None:
        """Already holding enough means nothing moves, however large the number asked for."""
        plugin = self._binance(capital="100", spot=900.0)
        moved = []
        plugin._write_transport.transfer = lambda d, a: moved.append((d, a))
        result = plugin.ensure_funds(80.0, "USDT")
        self.assertTrue(result.satisfied)
        self.assertEqual(result.action, "none")
        self.assertEqual(moved, [], "a target already met must not move money")
        self.assertIsNone(plugin.funding_requests.pending(), "and raises no request")

    def test_an_ask_moves_nothing_until_the_operator_approves(self) -> None:
        """Money leaving on the ask alone would be a transfer at a moment nobody chose."""
        plugin = self._binance(capital="30", spot=900.0)
        moved = []
        plugin._write_transport.transfer = lambda d, a: moved.append((d, float(a)))
        result = plugin.ensure_funds(100.0, "USDT")
        self.assertFalse(result.satisfied)
        self.assertEqual(result.action, "awaiting_operator_approval")
        self.assertEqual(moved, [], "nothing moves before approval")
        self.assertEqual(plugin.funding_requests.pending()["amount"], 100.0)

    def test_approval_moves_only_the_shortfall(self) -> None:
        plugin = self._binance(capital="30", spot=900.0)
        moved = []
        plugin._write_transport.transfer = lambda d, a: moved.append((d, float(a)))
        plugin.ensure_funds(100.0, "USDT")
        answer = plugin.approve_funding()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(moved, [("INBOUND", 70.0)], "target 100 against 30 held is a 70 top-up")
        self.assertIsNone(plugin.funding_requests.pending(), "an approved request is cleared")

    def test_a_later_ask_replaces_the_earlier_one(self) -> None:
        """Asking for 50 then 200 wants 200, not 250; approving a queue would approve history."""
        plugin = self._binance(capital="0", spot=900.0)
        plugin.ensure_funds(50.0, "USDT")
        plugin.ensure_funds(200.0, "USDT")
        self.assertEqual(plugin.funding_requests.pending()["amount"], 200.0)

    def test_a_dismissed_request_leaves_nothing_pending(self) -> None:
        plugin = self._binance(capital="10", spot=900.0)
        plugin.ensure_funds(100.0, "USDT")
        plugin.reject_funding({"note": "not this week"})
        self.assertIsNone(plugin.funding_requests.pending())

    def test_a_plugin_that_cannot_fund_says_so_instead_of_raising(self) -> None:
        from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin
        from tests._support import POLYMARKET_ENV

        plugin = PolymarketApiPlugin({**POLYMARKET_ENV, "POLYMARKET_TRADING_CAPITAL": "5"})
        plugin._write_transport.collateral_balance = lambda: 5.0
        plugin._write_transport.deposit_target = lambda: {
            "wallet_address": "0xabc", "signer_address": "0xdef", "wallet_type": "proxy",
            "collateral_token": "0xUSDC", "self_funding_possible": False,
        }
        result = plugin.ensure_funds(50.0, "pUSD")
        self.assertFalse(result.satisfied)
        self.assertEqual(result.action, "awaiting_external_transfer")
        self.assertIn("0xabc", result.detail, "say where the money has to go")

    def test_a_confirmation_is_checked_against_the_balance_not_believed(self) -> None:
        from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin
        from tests._support import POLYMARKET_ENV

        plugin = PolymarketApiPlugin({**POLYMARKET_ENV, "POLYMARKET_TRADING_CAPITAL": "5"})
        plugin._write_transport.collateral_balance = lambda: 5.0
        plugin._write_transport.deposit_target = lambda: {
            "wallet_address": "0xabc", "signer_address": "0xdef", "wallet_type": "proxy",
            "collateral_token": "0xUSDC", "self_funding_possible": False,
        }
        plugin.ensure_funds(50.0, "pUSD")
        refused = plugin.confirm_funding()
        self.assertFalse(refused["ok"])
        self.assertIn("short of", refused["message"])
        self.assertIsNotNone(plugin.funding_requests.pending(), "an unmet request stays open")

        plugin._write_transport.collateral_balance = lambda: 50.0
        accepted = plugin.confirm_funding()
        self.assertTrue(accepted["ok"], accepted)
        self.assertIsNone(plugin.funding_requests.pending())


class FrameworkStaysGenericTests(unittest.TestCase):
    def test_the_framework_names_no_platform_and_assumes_no_currency(self) -> None:
        """Venue knowledge belongs in the plugin; the framework only speaks the contract."""
        import pathlib, re

        root = pathlib.Path(__file__).resolve().parents[1] / "src/prediction_market_agent"
        offenders = []
        for area in ("core", "plugin_system", "runtime"):
            for path in (root / area).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for term in ("binance", "polymarket"):
                    if re.search(rf"\b{term}\b", text, re.I):
                        offenders.append(f"{area}/{path.name}:{term}")
        self.assertEqual(offenders, [])


class FundingLifecycleTests(unittest.TestCase):
    """An ask that outlives its call has to be findable again, and answerable with words."""

    def _binance(self, capital="10", spot=900.0):
        import tempfile, pathlib
        from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
        from tests._support import BINANCE_ENV

        directory = tempfile.mkdtemp()
        plugin = BinancePredictionApiPlugin({
            **BINANCE_ENV, "BINANCE_TRADING_CAPITAL": capital,
            "BINANCE_FUNDING_REQUEST_FILE": str(pathlib.Path(directory) / "r.json"),
        })
        plugin.client.spot_balances = lambda: {"USDT": {"free": spot, "locked": 0.0}}
        plugin._write_transport.transfer = lambda d, a: None
        return plugin

    def test_a_pending_ask_can_be_found_again_by_its_id(self) -> None:
        plugin = self._binance()
        first = plugin.ensure_funds(100.0, "USDT", reason="one")
        self.assertEqual(plugin.funding_status(first.request_id).state, "pending")

    def test_an_unknown_id_is_reported_rather_than_guessed_at(self) -> None:
        self.assertEqual(self._binance().funding_status("nope").state, "failed")

    def test_a_superseded_ask_learns_it_will_never_complete(self) -> None:
        """A caller still holding the old id must not wait forever on a replaced request."""
        plugin = self._binance()
        first = plugin.ensure_funds(50.0, "USDT", reason="one")
        second = plugin.ensure_funds(200.0, "USDT", reason="two")
        self.assertEqual(plugin.funding_status(first.request_id).state, "refused")
        self.assertEqual(plugin.funding_status(second.request_id).state, "pending")

    def test_a_request_past_its_deadline_is_dead_not_pending(self) -> None:
        """Approving hours later would fund an intention that no longer exists."""
        plugin = self._binance()
        asked = plugin.ensure_funds(100.0, "USDT", reason="one", timeout_seconds=0)
        import time as _time
        _time.sleep(0.01)
        self.assertIsNone(plugin.funding_requests.pending())
        self.assertEqual(plugin.funding_status(asked.request_id).state, "failed")

    def test_a_caller_that_cannot_wait_is_told_so_rather_than_parked(self) -> None:
        plugin = self._binance()
        answer = plugin.ensure_funds(100.0, "USDT", reason="one", allow_pending=False)
        self.assertEqual(answer.state, "failed")
        self.assertIsNone(plugin.funding_requests.pending(), "and nothing is left waiting")

    def test_the_reason_the_robot_gave_reaches_the_person_approving(self) -> None:
        plugin = self._binance()
        plugin.ensure_funds(100.0, "USDT", reason="BTC mispriced by 8 points")
        self.assertEqual(
            plugin.funding_requests.pending()["reason"], "BTC mispriced by 8 points"
        )

    def test_refusing_without_a_reason_is_refused_by_the_plugin(self) -> None:
        """A bare no would have the robot ask again identically on the next cycle."""
        plugin = self._binance()
        plugin.ensure_funds(100.0, "USDT", reason="one")
        for values in ({}, {"note": "   "}):
            with self.subTest(values=values):
                answer = plugin.reject_funding(values)
                self.assertFalse(answer["ok"])
                self.assertIsNotNone(plugin.funding_requests.pending(), "the ask stays open")

    def test_what_the_person_said_reaches_the_robot_either_way(self) -> None:
        for verb, note in (("approve", "this is the last of it"), ("reject", "not this week")):
            with self.subTest(verb=verb):
                plugin = self._binance()
                asked = plugin.ensure_funds(100.0, "USDT", reason="one")
                if verb == "approve":
                    plugin.approve_funding({"note": note})
                else:
                    plugin.reject_funding({"note": note})
                self.assertEqual(plugin.funding_status(asked.request_id).operator_note, note)


class NothingStopsAnUnfundedBuyTests(unittest.TestCase):
    """The framework does not check affordability, so what does has to be stated, not assumed."""

    def test_an_order_with_no_cash_reaches_the_venue(self) -> None:
        """Recorded because it is surprising: the venue refuses it, nothing here does."""
        from prediction_market_agent.core.domain import AccountState
        from prediction_market_agent.runtime.broker import ExecutionGateway

        sent = []

        class Transport:
            def get_quote(inner, **v):
                return {"quoteId": "q", "averagePrice": v["reference_price"],
                        "expireAt": 4_102_444_800_000}
            def place_order(inner, **v):
                sent.append("order")
                return {"orderId": "o", "status": "FILLED"}

        state = AccountState(starting_capital=0.0, cash=0.0)
        gateway = ExecutionGateway(state, platform="p", write_transport=Transport())
        quote = gateway.get_quote(
            token_id="t", side="BUY", price=0.5, quantity=20, fee_bps=0,
            market_topic_id=1, market_id=2, symbol="S", direction="UP", order_type="MARKET",
        )
        gateway.place_order(quote)
        self.assertEqual(sent, ["order"])
        self.assertLess(state.cash, 0)

    def test_the_built_in_strategy_tells_the_model_to_check_and_ask(self) -> None:
        """Since nothing enforces it, the instruction is the only thing standing there."""
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        text = BuiltInDecisionStrategy().instructions
        for expected in ("ACCOUNT_FUNDS", "ENSURE_FUNDS", "FUNDING_STATUS"):
            with self.subTest(tool=expected):
                self.assertIn(expected, text)
        self.assertIn("A pending request is not funding", text)
