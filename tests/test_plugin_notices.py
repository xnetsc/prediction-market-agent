"""Plugins tell the operator things; the framework renders them without understanding any of it.

A plugin's configuration page could only ask questions, never answer one. So a plugin needing an
operator to see a number, or to agree to something, had nowhere to put it and either acted alone or
failed quietly. It is asked in real time instead, and answers with whatever it currently has - text
to read, or a request with the buttons it wants offered and the words on them.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugin_system.discovery import PluginSpec, validated_notices
from prediction_market_agent.plugin_system.management import PluginManagementService


class NoticeShapeTests(unittest.TestCase):
    def test_a_plugin_with_nothing_to_say_shows_nothing(self) -> None:
        self.assertEqual(validated_notices([]), [])
        self.assertEqual(validated_notices(None), [])

    def test_a_message_to_read_needs_no_buttons_at_all(self) -> None:
        [notice] = validated_notices(
            [{"key": "k", "title": "Heads up", "kind": "display", "content": {"a": 1}}]
        )
        self.assertEqual(notice["kind"], "display")
        self.assertEqual(notice.get("action_label", ""), "")

    def test_a_request_carries_both_answers_and_the_plugin_words_them(self) -> None:
        """Agreeing and refusing are the plugin's to name; the framework only draws the buttons."""
        [notice] = validated_notices([{
            "key": "funding", "title": "Move money?", "kind": "confirm",
            "content": {"amount": 50},
            "action_label": "批准并转账", "dismiss_label": "拒绝",
        }])
        self.assertEqual(notice["action_label"], "批准并转账")
        self.assertEqual(notice["dismiss_label"], "拒绝")

    def test_a_request_with_no_label_is_refused_rather_than_drawn(self) -> None:
        """A button with no words is one the operator cannot answer, so this fails loudly."""
        with self.assertRaisesRegex(ValueError, "button label"):
            validated_notices([{"key": "k", "title": "t", "kind": "confirm", "content": {}}])

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "kind"):
            validated_notices([{"key": "k", "title": "t", "kind": "banner", "content": {}}])

    def test_a_notice_without_identity_is_refused(self) -> None:
        for item in ({"title": "t", "kind": "display"}, {"key": "k", "kind": "display"}):
            with self.subTest(item=item):
                with self.assertRaisesRegex(ValueError, "key and a title"):
                    validated_notices([item])


class NoticeDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = PluginManagementService(
            Config(management_file=Path(self.temp.name) / "managed.json")
        )
        self.answered: list[tuple[str, str, dict]] = []

    def _install(self, notices, action=None) -> None:
        spec = PluginSpec(
            kind="risk", name="sample", description="A sample plugin", origin="/tmp/sample.py",
            factory=lambda config, services: None, teardown=lambda: None,
            notices_callback=notices,
            notice_action_callback=action,
        )
        self.service.catalog.register(spec)

    def test_the_framework_asks_and_relays_what_it_is_told(self) -> None:
        self._install(lambda: [{"key": "k", "title": "t", "kind": "display", "content": {"n": 1}}])
        answer = self.service.notice_content("risk", "sample")
        self.assertEqual(answer["notices"][0]["content"], {"n": 1})

    def test_a_plugin_is_asked_fresh_every_time(self) -> None:
        """What a plugin needs to say depends on what has happened to it since it was last asked."""
        calls = []
        self._install(lambda: calls.append(1) or [])
        self.service.notice_content("risk", "sample")
        self.service.notice_content("risk", "sample")
        self.assertEqual(len(calls), 2)

    def test_a_plugin_that_breaks_does_not_take_its_page_down(self) -> None:
        """The configuration form the operator came here to fill in still has to render."""
        self._install(lambda: (_ for _ in ()).throw(RuntimeError("credentials missing")))
        answer = self.service.notice_content("risk", "sample")
        self.assertIn("credentials missing", answer["error"])
        self.assertEqual(answer["notices"], [])

    def test_both_answers_reach_the_plugin_as_distinct_verbs(self) -> None:
        self._install(
            lambda: [{"key": "k", "title": "t", "kind": "confirm", "content": {},
                      "action_label": "同意", "dismiss_label": "拒绝"}],
            action=lambda key, verb, values: self.answered.append((key, verb, values)) or {"ok": True},
        )
        self.service.notice_action("risk", "sample", "k", "confirm", {})
        self.service.notice_action("risk", "sample", "k", "dismiss", {})
        self.assertEqual([verb for _, verb, _ in self.answered], ["confirm", "dismiss"])

    def test_a_plugin_offering_no_actions_says_so(self) -> None:
        self._install(lambda: [])
        with self.assertRaisesRegex(ValueError, "no notice actions"):
            self.service.notice_action("risk", "sample", "k", "confirm", {})

    def test_a_plugin_that_was_never_asked_to_speak_reports_nothing(self) -> None:
        self._install(None)
        self.assertEqual(self.service.notice_content("risk", "sample")["notices"], [])


if __name__ == "__main__":
    unittest.main()


class CredentialHonestyTests(unittest.TestCase):
    """What a plugin cannot run without must be said once, where the page can see it."""

    def _catalog(self):
        from prediction_market_agent.plugin_system.discovery import load_plugin_catalog

        catalog = load_plugin_catalog(Config.load())
        self.addCleanup(catalog.shutdown)
        return catalog

    def test_credentials_are_marked_as_needed_to_run(self) -> None:
        """Shown as optional while the plugin refuses to start without them is the lie to avoid."""
        expected = {
            "binance": {
                "BINANCE_API_KEY", "BINANCE_API_SECRET",
                "BINANCE_PREDICTION_WALLET_ADDRESS", "BINANCE_PREDICTION_WALLET_ID",
            },
            # The CLOB credentials and the funder address are consequences of the signing key,
            # so the plugin derives them and must not be asking for them as well.
            "polymarket": {"POLYMARKET_PRIVATE_KEY"},
        }
        catalog = self._catalog()
        for name, names in expected.items():
            with self.subTest(plugin=name):
                fields = catalog.get("api", name).configuration.fields
                marked = {f.name for f in fields if f.needed_to_run}
                self.assertEqual(marked, names)

    def test_readiness_reports_exactly_what_the_schema_marked(self) -> None:
        """One declaration, two readers. Two lists of the same fact drift, and one did."""
        catalog = self._catalog()
        for name in ("binance", "polymarket"):
            with self.subTest(plugin=name):
                spec = catalog.get("api", name)
                stored = spec.configuration.load()
                expected = {
                    f.name
                    for f in spec.configuration.fields
                    if f.needed_to_run and not str(stored.get(f.name, "")).strip()
                }
                reasons = spec.readiness_callback().reasons
                reported = {r.replace("Missing runtime field: ", "") for r in reasons}
                self.assertEqual(reported, expected)

    def test_nothing_fixed_and_public_is_left_for_the_operator_to_type(self) -> None:
        """An endpoint the plugin already knows is a typo waiting to point credentials elsewhere."""
        from prediction_market_agent.plugin_system.discovery import UNSET

        catalog = self._catalog()
        for name in ("binance", "polymarket"):
            with self.subTest(plugin=name):
                blank = [
                    f.name
                    for f in catalog.get("api", name).configuration.fields
                    if f.required and (f.default is UNSET or f.default in ("", None))
                ]
                self.assertEqual(blank, [])

    def test_the_polymarket_endpoints_come_from_the_official_client(self) -> None:
        """Copied by hand they would drift the day the venue moves one."""
        from polymarket import PRODUCTION

        fields = {f.name: f.default for f in self._catalog().get("api", "polymarket").configuration.fields}
        self.assertEqual(fields["POLYMARKET_GAMMA_URL"], PRODUCTION.gamma_url)
        self.assertEqual(fields["POLYMARKET_CLOB_URL"], PRODUCTION.clob_url)
        self.assertEqual(fields["POLYMARKET_RELAYER_URL"], PRODUCTION.relayer_url)
        self.assertEqual(fields["POLYMARKET_CHAIN_ID"], PRODUCTION.chain_id)


class AButtonCanSayItIsNotUsableYetTests(unittest.TestCase):
    """Waiting for a chain confirmation is the plugin's business; the button is the framework's."""

    def test_a_plugin_may_grey_out_its_own_button_and_say_why(self) -> None:
        [notice] = validated_notices([{
            "key": "deposit", "title": "我要充值", "kind": "confirm",
            "content": {}, "action_label": "我已充值，去查",
            "action_disabled": True, "action_note": "确认中（4/12 个确认）",
        }])
        self.assertTrue(notice["action_disabled"])
        self.assertEqual(notice["action_note"], "确认中（4/12 个确认）")

    def test_an_ordinary_notice_has_a_usable_button(self) -> None:
        [notice] = validated_notices([{
            "key": "k", "title": "t", "kind": "confirm", "content": {}, "action_label": "去",
        }])
        self.assertFalse(notice["action_disabled"])
        self.assertEqual(notice["action_note"], "")

    def test_the_page_draws_both_and_asks_again_after_every_answer(self) -> None:
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn("notice.action_disabled", views)
        self.assertIn("notice.action_note", views)
        self.assertIn("function noticeContentHtml", views)
        after = views[views.index("const answer=await post('/api/plugins/notices/action'"):]
        handler = after[:after.index("}catch(e)")]
        self.assertIn("renderPluginNotices(card,kind,plugin);", handler,
                      "a panel that only redraws on success cannot show 'still confirming'")
        self.assertIn("answer.pending?'pending':'danger'", handler,
                      "a wait drawn in red reads as a mistake the operator made")


class DepositingIsOfferedWheneverThePluginIsLoadedTests(unittest.TestCase):
    """Being asked for money is one way to deposit; deciding to is the other, and it comes first."""

    def _plugin_source(self) -> str:
        return Path("src/prediction_market_agent/plugins/api/polymarket.py").read_text()

    def test_the_deposit_notice_does_not_wait_for_the_robot_to_ask(self) -> None:
        source = self._plugin_source()
        self.assertIn("def deposit_notices()", source)
        self.assertIn("*deposit_notices()", source)
        block = source[source.index("def deposit_notices()"):source.index("def funding_notices()")]
        self.assertIn('"key": "deposit"', block)
        self.assertIn("我要充值", block)
        self.assertNotIn("pending_request", block, "this one is offered whether or not one is open")

    def test_the_robots_request_confirms_through_the_same_deposit_check(self) -> None:
        source = self._plugin_source()
        block = source[source.index("def funding_action("):source.index("return PluginSpec(")]
        self.assertIn('deposit_action("funding", "confirm", payload)', block)
        funding = source[source.index("def funding_notices()"):source.index("def wallet_notices()")]
        self.assertIn("*DEPOSIT_FIELDS", funding, "the same txid field, so it is the same act")

    def test_what_a_deposit_needs_is_stated_rather_than_guessed(self) -> None:
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_instructions("):write.index("def _token_identity(")]
        for field in ("chain", "chain_id", "address", "deposit_currency",
                      "account_token_symbol", "account_token_contract", "warnings"):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', block)

    def test_the_instructions_do_not_invent_the_one_thing_they_cannot_know(self) -> None:
        """Which USDC contract this platform credits is not ours to guess; the rest is certain."""
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_instructions("):write.index("def _token_identity(")]
        self.assertIn("以 Polymarket 官网充值页当时显示的为准", block)
        self.assertIn("先转一小笔", block)
        self.assertIn("deposit_currency", block)

    def test_a_deposit_is_followed_on_the_chain_not_taken_on_trust(self) -> None:
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_status("):write.index("def deposit_target(")]
        for state in ("not_found", "failed", "confirming", "arrived", "wrong_target"):
            with self.subTest(state=state):
                self.assertIn(f'"{state}"', block)
        self.assertIn("eth_getTransactionReceipt", block)
        self.assertIn("eth_blockNumber", block)


class BothVenuesOfferTheSameDepositFlowTests(unittest.TestCase):
    """Each venue states its own chain, coin and address; the framework only draws them."""

    def _source(self, name: str) -> str:
        return Path(f"src/prediction_market_agent/plugins/api/{name}.py").read_text()

    def test_binance_offers_depositing_without_waiting_to_be_asked(self) -> None:
        source = self._source("binance")
        self.assertIn("def deposit_notices()", source)
        self.assertIn("*deposit_notices()", source)
        block = source[source.index("def deposit_notices()"):source.index("def funding_notices()")]
        self.assertIn("我要充值", block)
        self.assertNotIn("pending_request", block)

    def test_the_exchange_is_asked_which_chains_it_takes_rather_than_told(self) -> None:
        """Which networks an exchange credits changes; a remembered list deposits to the wrong one."""
        read = Path("src/prediction_market_agent/plugins/api/_binance/read.py").read_text()
        self.assertIn("/sapi/v1/capital/config/getall", read)
        self.assertIn("/sapi/v1/capital/deposit/address", read)
        self.assertIn("/sapi/v1/capital/deposit/hisrec", read)

    def test_binance_says_a_deposit_is_not_yet_spendable_on_predictions(self) -> None:
        """It lands in the exchange account; a transfer makes it tradeable, and that is a step."""
        adapter = Path("src/prediction_market_agent/plugins/api/_binance/adapter.py").read_text()
        panel = adapter[adapter.index("def deposit_panel("):adapter.index("def deposit_status(")]
        self.assertIn("再由这里划转进预测钱包", panel)
        confirm = adapter[adapter.index("def confirm_deposit("):adapter.index("def funding_panel(")]
        self.assertIn("还需要一次划转", confirm)

    def test_a_deposit_offered_to_answer_a_request_is_checked_before_money_moves(self) -> None:
        source = self._source("binance")
        block = source[source.index("def funding_action("):source.index("return PluginSpec(")]
        self.assertIn('deposit_action("funding", "confirm", payload)', block)
        self.assertIn("if not answer.get(\"ok\"):", block)

    def test_every_deposit_state_the_exchange_reports_is_translated(self) -> None:
        adapter = Path("src/prediction_market_agent/plugins/api/_binance/adapter.py").read_text()
        block = adapter[adapter.index("def deposit_status("):adapter.index("def confirm_deposit(")]
        for state in ("invalid", "not_found", "confirming", "wrong_target", "arrived"):
            with self.subTest(state=state):
                self.assertIn(f'"{state}"', block)


class WhatTheBinanceDepositPanelActuallySaysTests(unittest.TestCase):
    """Run the panel against a stand-in exchange: the wrong chain is unrecoverable, so it is tested."""

    def _plugin(self, *, networks=None, history=None, address=None, fail=None):
        from types import SimpleNamespace

        from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
        from tests._support import BINANCE_ENV

        plugin = BinancePredictionApiPlugin({**BINANCE_ENV, "BINANCE_TRADING_CAPITAL": "0",
                                             "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"})
        asked: dict = {}

        def deposit_networks(coin):
            asked["coin"] = coin
            if fail == "networks":
                raise RuntimeError("Binance HTTP 451")
            return networks if networks is not None else [
                {"network": "BSC", "name": "BNB Smart Chain", "deposit_open": True,
                 "minimum_confirmations": 15, "confirmations_before_withdrawal": 15,
                 "contract": "0x55d", "needs_memo": False, "note": "", "default": False},
                {"network": "MATIC", "name": "Polygon", "deposit_open": True,
                 "minimum_confirmations": 300, "confirmations_before_withdrawal": 300,
                 "contract": "0xc21", "needs_memo": False, "note": "手续费低", "default": True},
                {"network": "TRX", "name": "Tron", "deposit_open": False,
                 "minimum_confirmations": 1, "confirmations_before_withdrawal": 1,
                 "contract": "", "needs_memo": True, "note": "", "default": False},
            ]

        def deposit_address(coin, network=""):
            asked["network"] = network
            return address or {"address": f"addr-{network}", "memo": "", "coin": coin,
                               "network": network, "url": ""}

        plugin.client = SimpleNamespace(
            deposit_networks=deposit_networks,
            deposit_address=deposit_address,
            deposit_history=lambda coin="", limit=50: history or [],
            spot_balances=lambda: {"USDT": {"free": 40.0, "locked": 0.0}},
        )
        plugin.asked = asked
        return plugin

    def test_the_default_chain_is_used_and_the_others_are_listed(self) -> None:
        plugin = self._plugin()
        panel = plugin.deposit_panel()
        self.assertEqual(plugin.asked["coin"], "USDT", "this account settles in USDT, so that is the coin")
        self.assertEqual(plugin.asked["network"], "MATIC", "the exchange's own default")
        self.assertEqual(panel["收款地址"], "addr-MATIC")
        self.assertEqual(panel["到账需要确认数"], 300)
        listed = " ".join(panel["可充值网络"])
        self.assertIn("BSC", listed)
        self.assertIn("MATIC", listed)
        self.assertNotIn("TRX", listed, "a chain the exchange has closed is not an option")

    def test_a_chain_that_needs_a_memo_says_so_where_it_is_used(self) -> None:
        plugin = self._plugin(
            networks=[{"network": "XRP", "name": "Ripple", "deposit_open": True,
                       "minimum_confirmations": 1, "confirmations_before_withdrawal": 1,
                       "contract": "", "needs_memo": True, "note": "", "default": True}],
            address={"address": "r9y", "memo": "12345", "coin": "USDT", "network": "XRP", "url": ""},
        )
        panel = plugin.deposit_panel()
        self.assertIn("memo / tag", panel)
        self.assertIn("钱到不了", panel["memo / tag"])

    def test_the_operator_can_ask_for_another_chains_address(self) -> None:
        plugin = self._plugin()
        panel = plugin.deposit_panel("bsc")
        self.assertEqual(plugin.asked["network"], "BSC")
        self.assertEqual(panel["收款地址"], "addr-BSC")

    def test_an_exchange_that_will_not_answer_says_so_instead_of_an_address(self) -> None:
        panel = self._plugin(fail="networks").deposit_panel()
        self.assertIn("读取可充值网络失败", panel)
        self.assertNotIn("收款地址", panel, "an address nobody confirmed is worse than none")

    def test_each_status_the_exchange_reports_becomes_something_actionable(self) -> None:
        cases = {0: "confirming", 8: "confirming", 1: "arrived", 6: "arrived", 7: "wrong_target"}
        for code, expected in cases.items():
            with self.subTest(status=code):
                plugin = self._plugin(history=[{"txId": "0xabc", "amount": "25", "network": "MATIC",
                                                "status": code, "confirmTimes": "10/300"}])
                self.assertEqual(plugin.deposit_status("0xABC")["state"], expected,
                                 "the transaction id is matched regardless of case")

    def test_a_transaction_the_exchange_has_not_seen_is_not_called_arrived(self) -> None:
        plugin = self._plugin(history=[{"txId": "0xother", "amount": "5", "status": 1}])
        status = plugin.deposit_status("0xabc")
        self.assertEqual(status["state"], "not_found")
        self.assertIn("还没被它看到", status["detail"])

    def test_arriving_is_not_the_same_as_being_spendable(self) -> None:
        """It lands in the exchange account; predictions trade out of a different wallet."""
        plugin = self._plugin(history=[{"txId": "0xabc", "amount": "25", "network": "MATIC",
                                        "status": 1, "confirmTimes": "300/300"}])
        answer = plugin.confirm_deposit({"txid": "0xabc"})
        self.assertTrue(answer["ok"])
        self.assertIn("还需要一次划转", answer["message"])
        self.assertIn("现货可划转 40.0", answer["message"])

    def test_still_confirming_is_a_wait_not_a_failure(self) -> None:
        plugin = self._plugin(history=[{"txId": "0xabc", "amount": "25", "status": 0,
                                        "confirmTimes": "2/300", "network": "MATIC"}])
        answer = plugin.confirm_deposit({"txid": "0xabc"})
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["pending"])
        self.assertIn("2/300", answer["message"])
