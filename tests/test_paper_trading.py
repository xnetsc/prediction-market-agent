from ._support import *

from prediction_market_agent.runtime.paper_trading import (
    PaperMarketApi,
    PaperWriteTransport,
    SIMULATED,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def get_quote(self, **values):
        self.sent.append(("get_quote", values))
        return {"quoteId": "q-1", "price": 0.42, "feeBps": 30, "expiresAt": 1}

    def place_order(self, **values):
        self.sent.append(("place_order", values))
        return {"orderId": "real-1", "status": "FILLED"}

    def cancel_orders(self, order_ids):
        self.sent.append(("cancel_orders", {"ids": order_ids}))
        return {"canceled": order_ids}

    def redeem(self, outcome_ids):
        self.sent.append(("redeem", {"ids": outcome_ids}))
        return {"redeemed": outcome_ids}

    def transfer(self, direction, amount):
        self.sent.append(("transfer", {"direction": direction, "amount": amount}))
        return {"ok": True}

    def deposit_target(self):
        return {"wallet_address": "0xabc"}


class WhatStillReachesThePlatformTests(unittest.TestCase):
    """Everything real except the part that spends money - which means the price must be real."""

    def setUp(self) -> None:
        self.real = FakeTransport()
        self.paper = PaperWriteTransport(self.real)

    def test_the_quote_is_the_venues_own(self) -> None:
        """A guessed price makes every number downstream fiction."""
        quote = self.paper.get_quote(token_id="t", side="BUY", notional=10)
        self.assertEqual(quote["price"], 0.42)
        self.assertEqual([name for name, _ in self.real.sent], ["get_quote"])

    def test_no_write_ever_reaches_the_platform(self) -> None:
        self.paper.place_order(quote_id="q-1", order_type="MARKET", price_limit=None)
        self.paper.cancel_orders(["paper-x"])
        self.paper.redeem(["token"])
        self.paper.transfer("OUTBOUND", "5")
        self.assertEqual([name for name, _ in self.real.sent], [], "money moved at the venue")

    def test_a_fill_looks_like_a_fill_and_says_it_is_simulated(self) -> None:
        order = self.paper.place_order(quote_id="q-1", order_type="MARKET", price_limit=None)
        self.assertEqual(order["status"], "FILLED")
        self.assertTrue(order["orderId"].startswith("paper-"))
        self.assertTrue(order["simulated"])

    def test_cancelling_an_order_that_was_never_placed_is_reported_not_pretended(self) -> None:
        answer = self.paper.cancel_orders(["never-existed"])
        self.assertEqual(answer["canceled"], [])
        self.assertEqual(answer["failed"][0]["orderId"], "never-existed")

    def test_anything_outside_the_write_contract_still_belongs_to_the_plugin(self) -> None:
        self.assertEqual(self.paper.deposit_target()["wallet_address"], "0xabc")


class FakePlugin:
    name = "venue"
    capabilities = SimpleNamespace(settlement_status=True)

    def __init__(self, real_available: float = 0.0, funds_error: str = ""):
        self._real_available = real_available
        self._funds_error = funds_error
        self.transport = FakeTransport()

    def account_funds(self):
        from prediction_market_agent.plugin_system.contracts import AccountFunds

        if self._funds_error:
            raise RuntimeError(self._funds_error)
        return AccountFunds(
            available=self._real_available, currency="USDT", source="platform"
        )

    def create_write_gateway(self, state):
        del state
        return SimpleNamespace(write_transport=self.transport)

    def outcome_won(self, detail, market, outcome):
        return True

    def something_the_contract_never_mentioned(self):
        return "still here"


class WhatTheAccountIsToldTests(unittest.TestCase):
    """A robot told it has nothing correctly does nothing, and teaches nobody anything."""

    def test_the_declared_figure_is_what_it_can_size_against(self) -> None:
        funds = PaperMarketApi(FakePlugin(), 500.0).account_funds()
        self.assertEqual(funds.available, 500.0)

    def test_it_is_never_passed_off_as_the_platforms_answer(self) -> None:
        """The one failure that would make this worse than useless."""
        funds = PaperMarketApi(FakePlugin(), 500.0).account_funds()
        self.assertEqual(funds.source, SIMULATED)
        self.assertNotEqual(funds.source, "platform")
        self.assertTrue(funds.detail["paper_trading"])

    def test_the_real_balance_is_reported_beside_it(self) -> None:
        funds = PaperMarketApi(FakePlugin(real_available=7.5), 500.0).account_funds()
        self.assertEqual(funds.detail["platform_actually_has"], 7.5)

    def test_an_unreadable_real_balance_does_not_stop_a_paper_run(self) -> None:
        funds = PaperMarketApi(FakePlugin(funds_error="geo blocked"), 500.0).account_funds()
        self.assertEqual(funds.available, 500.0)
        self.assertIn("geo blocked", funds.detail["platform_balance_error"])

    def test_funding_is_granted_up_to_the_figure_and_refused_past_it(self) -> None:
        """Silently granting anything would hide what it does when it cannot have what it asked."""
        api = PaperMarketApi(FakePlugin(), 500.0)
        self.assertTrue(api.ensure_funds(100.0, "USDT").satisfied)
        refused = api.ensure_funds(900.0, "USDT")
        self.assertFalse(refused.satisfied)
        self.assertEqual(refused.state, "refused")
        self.assertIn("500", refused.detail)

    def test_reads_capabilities_and_settlement_pass_straight_through(self) -> None:
        api = PaperMarketApi(FakePlugin(), 500.0)
        self.assertTrue(api.capabilities.settlement_status)
        self.assertTrue(api.outcome_won(None, None, None), "settlement must use the real answer")
        self.assertEqual(api.something_the_contract_never_mentioned(), "still here")

    def test_the_gateway_it_hands_back_cannot_reach_the_venue(self) -> None:
        plugin = FakePlugin()
        gateway = PaperMarketApi(plugin, 500.0).create_write_gateway(object())
        gateway.write_transport.place_order(quote_id="q", order_type="MARKET", price_limit=None)
        self.assertEqual(plugin.transport.sent, [])


class TheSwitchTests(unittest.TestCase):
    """Live trading is what happens when nobody turned this on."""

    def test_it_is_off_unless_chosen(self) -> None:
        self.assertFalse(Config().paper_trading)

    def test_the_setting_is_offered_with_the_money_it_needs(self) -> None:
        from prediction_market_agent.core.config import APPLICATION_FIELDS

        fields = {field.name: field for field in APPLICATION_FIELDS}
        self.assertEqual(fields["paper_trading"].default, "off")
        self.assertEqual(set(fields["paper_trading"].options), {"off", "on"})
        self.assertGreater(int(fields["paper_trading_funds"].default), 0)

    def test_bootstrap_wraps_the_platforms_only_when_it_is_on(self) -> None:
        import inspect
        from prediction_market_agent.runtime import bootstrap

        source = inspect.getsource(bootstrap)
        self.assertIn("PaperMarketApi(item, config.paper_trading_funds) if config.paper_trading",
                      source)
        wrap = source[source.index("prepared = ["):]
        self.assertIn("GuardedMarketApi(item, risk) for item in prepared",
                      wrap, "the filter plugins must still see every call")


class SettlementReachesHeldPositionsTests(unittest.TestCase):
    """Bought and never resolved is the failure that makes a paper run unreadable."""

    def test_the_cycle_goes_looking_for_what_it_holds(self) -> None:
        """Settlement used to need discovery to hand back an expired topic, which venues do not."""
        import inspect
        from prediction_market_agent.runtime.engine import TradingEngine

        cycle = inspect.getsource(TradingEngine._process_platform_topics)
        self.assertIn("_settle_open_positions(runtime)", cycle)
        sweep = inspect.getsource(TradingEngine._settle_open_positions)
        self.assertIn("runtime.state.positions", sweep)
        self.assertIn("_settle_if_possible", sweep)

    def test_a_market_still_running_is_left_alone(self) -> None:
        import inspect
        from prediction_market_agent.runtime.engine import TradingEngine

        sweep = inspect.getsource(TradingEngine._settle_open_positions)
        self.assertIn("end_time_ms >", sweep, "an open market must not be settled")

    def test_one_unreachable_topic_does_not_stop_the_others(self) -> None:
        import inspect
        from prediction_market_agent.runtime.engine import TradingEngine

        sweep = inspect.getsource(TradingEngine._settle_open_positions)
        self.assertIn("continue", sweep)
        self.assertIn("LOGGER.warning", sweep)


class PolymarketCanReportAWinnerTests(unittest.TestCase):
    """Nothing settles on a platform that says it cannot tell you who won."""

    def _plugin(self, event):
        from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin

        plugin = PolymarketApiPlugin.__new__(PolymarketApiPlugin)
        plugin.client = SimpleNamespace(get_event=lambda topic_id: event)
        return plugin

    def _args(self, outcome_name="Yes"):
        detail = SimpleNamespace(topic=SimpleNamespace(topic_id="99"))
        market = SimpleNamespace(market_id="cond-1")
        return detail, market, SimpleNamespace(name=outcome_name, outcome_id="t")

    def _event(self, closed, prices):
        return {"markets": [{
            "conditionId": "cond-1", "closed": closed,
            "outcomes": '["Yes", "No"]', "outcomePrices": prices,
        }]}

    def test_the_platform_now_declares_it_can_report_settlement(self) -> None:
        from prediction_market_agent.plugins.api._polymarket import adapter
        import inspect

        self.assertIn("settlement_status=True", inspect.getsource(adapter))

    def test_a_resolved_winner_and_loser_are_both_read(self) -> None:
        event = self._event(True, '["0.9999995", "0.0000004"]')
        detail, market, outcome = self._args("Yes")
        self.assertIs(self._plugin(event).outcome_won(detail, market, outcome), True)
        detail, market, loser = self._args("No")
        self.assertIs(self._plugin(event).outcome_won(detail, market, loser), False)

    def test_a_market_still_trading_is_not_a_result(self) -> None:
        """0.97 is a market that can still be wrong; booking it would invent a profit."""
        detail, market, outcome = self._args("Yes")
        self.assertIsNone(
            self._plugin(self._event(True, '["0.97", "0.03"]')).outcome_won(detail, market, outcome)
        )
        self.assertIsNone(
            self._plugin(self._event(False, '["0.999", "0.001"]')).outcome_won(
                detail, market, outcome
            )
        )


class TheMoneyIsCheckedWhereAVenueChecksItTests(unittest.TestCase):
    """A decision is made on the market; whether it can be paid for is settled at the order."""

    def _paper(self, cash: float):
        from prediction_market_agent.core.domain import AccountState

        state = AccountState(starting_capital=cash, cash=cash)
        transport = PaperWriteTransport(FakeTransport(), state)
        return transport, state

    def test_a_buy_the_account_cannot_cover_is_refused_like_a_venue_would(self) -> None:
        paper, _ = self._paper(5.0)
        quote = paper.get_quote(outcome_id="t", side="BUY", amount="12", fee_bps=30)
        with self.assertRaises(RuntimeError) as refused:
            paper.place_order(quote_id=quote["quoteId"], order_type="MARKET", price_limit=None)
        self.assertIn("模拟资金不足", str(refused.exception))
        self.assertIn("12.04", str(refused.exception))
        self.assertEqual(paper.orders, {}, "a refused order is not a fill")

    def test_a_buy_it_can_cover_fills_and_a_sale_needs_no_money(self) -> None:
        paper, _ = self._paper(20.0)
        quote = paper.get_quote(outcome_id="t", side="BUY", amount="12", fee_bps=30)
        self.assertEqual(paper.place_order(quote_id=quote["quoteId"], order_type="MARKET", price_limit=None)["status"], "FILLED")
        broke, _ = self._paper(0.0)
        sale = broke.get_quote(outcome_id="t", side="SELL", amount="5", fee_bps=30)
        self.assertEqual(broke.place_order(quote_id=sale["quoteId"], order_type="MARKET", price_limit=None)["status"], "FILLED")

    def test_what_the_account_reports_is_what_is_left_not_what_was_declared(self) -> None:
        from prediction_market_agent.core.domain import AccountState

        plugin = SimpleNamespace(
            name="p", capabilities=None,
            account_funds=lambda: SimpleNamespace(available=0.0, currency="USDC"),
            create_write_gateway=lambda state: SimpleNamespace(write_transport=FakeTransport()),
        )
        api = PaperMarketApi(plugin, 100.0)
        self.assertEqual(api.account_funds().available, 100.0, "before trading, the declared figure")
        state = AccountState(starting_capital=100.0, cash=100.0)
        api.create_write_gateway(state)
        state.cash = 30.0
        funds = api.account_funds()
        self.assertEqual(funds.available, 30.0)
        self.assertEqual(funds.detail["declared"], 100.0)
        self.assertEqual(api.ensure_funds(25.0, "USDC").state, "satisfied")
        self.assertEqual(api.ensure_funds(40.0, "USDC").state, "refused")


class APaperRunKeepsItsOwnBookTests(unittest.TestCase):
    def test_the_paper_book_never_shares_the_live_file(self) -> None:
        from pathlib import Path
        from prediction_market_agent.plugin_system.contracts import platform_state_path

        live = Path("agent_state.json")
        self.assertEqual(platform_state_path(live, "polymarket", False), live)
        self.assertEqual(platform_state_path(live, "polymarket", False, paper=True), Path("agent_state.paper.json"))
        self.assertEqual(
            platform_state_path(live, "polymarket", True, paper=True), Path("agent_state-polymarket.paper.json")
        )

    def test_an_existing_book_opens_whatever_the_platform_reports_today(self) -> None:
        """The balance moves with every trade; the book's opening figure does not."""
        import tempfile
        from pathlib import Path
        from prediction_market_agent.core.state import StateStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path, 0.0)
            store.save(store.load())
            reopened = StateStore(path, 250.0).load()
            self.assertEqual(reopened.starting_capital, 0.0)
