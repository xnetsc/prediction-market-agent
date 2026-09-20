"""Plugins tell the operator things; the framework renders them without understanding any of it.

A plugin's configuration page could only ask questions, never answer one. So a plugin needing an
operator to see a number, or to agree to something, had nowhere to put it and either acted alone or
failed quietly. It is asked in real time instead, and answers with whatever it currently has - text
to read, or a request with the buttons it wants offered and the words on them.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_a_permanent_operation_can_opt_out_of_the_attention_banner(self) -> None:
        [notice] = validated_notices([{
            "key": "deposit", "title": "Deposit", "kind": "display",
            "content": {}, "attention": False,
        }])
        self.assertIs(notice["attention"], False)

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
        self.assertEqual(fields["POLYMARKET_BRIDGE_URL"], "https://bridge.polymarket.com")
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

    def test_a_notice_select_keeps_bound_labels_and_values(self) -> None:
        [notice] = validated_notices([{
            "key": "withdraw", "title": "转出", "kind": "confirm", "content": {},
            "action_label": "确认并转出", "action_fields": [{
                "name": "destination", "label": "目标网络与币种", "type": "select",
                "value": "137:0xabc", "options": [{
                    "value": "137:0xabc",
                    "label": "USD Coin / USDC · Polygon（chain id 137） · 合约 0xabc",
                }],
            }],
        }])
        [field] = notice["action_fields"]
        self.assertEqual(field["type"], "select")
        self.assertEqual(field["value"], "137:0xabc")
        self.assertIn("Polygon", field["options"][0]["label"])

    def test_the_page_draws_both_and_asks_again_after_every_answer(self) -> None:
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn("notice.action_disabled", views)
        self.assertIn("notice.action_note", views)
        self.assertIn("function noticeContentHtml", views)
        after = views[views.index("const answer=await post('/api/plugins/notices/action'"):]
        handler = after[:after.index("}catch(e)")]
        self.assertIn("renderPluginNotices(card,kind,plugin,null,{...filter,fresh:true});", handler,
                      "a panel that only redraws on success cannot show 'still confirming'")
        self.assertIn("answer.pending?'pending':'danger'", handler,
                      "a wait drawn in red reads as a mistake the operator made")

    def test_fund_notices_have_a_dedicated_page_and_leave_platform_config(self) -> None:
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        shell = Path("src/prediction_market_agent/runtime/static/dashboard-shell.js").read_text()
        self.assertIn("PLUGIN_FUNDS_NOTICE_KEYS", views)
        self.assertIn("exclude:PLUGIN_FUNDS_NOTICE_KEYS", views)
        self.assertIn("only:PLUGIN_FUNDS_NOTICE_KEYS", views)
        self.assertIn("async function refreshFunds", views)
        self.assertIn("funds: ['资金管理'", shell)


class DepositingIsOfferedWheneverThePluginIsLoadedTests(unittest.TestCase):
    """Being asked for money is one way to deposit; deciding to is the other, and it comes first."""

    def _plugin_source(self) -> str:
        return Path("src/prediction_market_agent/plugins/api/polymarket.py").read_text()

    def test_the_deposit_notice_does_not_wait_for_the_robot_to_ask(self) -> None:
        source = self._plugin_source()
        self.assertIn("def deposit_notices(funds=None)", source)
        self.assertIn("pool.submit(deposit_notices", source)
        block = source[
            source.index("def deposit_notices(funds=None)"):
            source.index("def funding_notices()")
        ]
        self.assertIn('"key": "deposit"', block)
        self.assertIn("我要充值", block)
        self.assertNotIn("pending_request", block, "this one is offered whether or not one is open")

    def test_a_paused_runtime_reuses_one_panel_adapter(self) -> None:
        source = self._plugin_source()
        block = source[source.index("def _live_instance()") : source.index("def funding_status()")]
        self.assertIn("instances.append(PolymarketApiPlugin(resolved_values()))", block)
        self.assertNotIn("return PolymarketApiPlugin(resolved_values())", block)

    def test_permanent_fund_controls_are_not_reported_as_pending_events(self) -> None:
        source = self._plugin_source()
        notices = source[source.index("def notices()") : source.index("return PluginSpec(")]
        for key in ("wallet", "wallet_backup", "deposit", "withdraw"):
            self.assertIn(f'"{key}"', notices)
        self.assertIn('item["attention"] = False', notices)

    def test_the_robots_request_confirms_through_the_same_deposit_check(self) -> None:
        source = self._plugin_source()
        block = source[source.index("def funding_action("):source.index("return PluginSpec(")]
        self.assertIn('deposit_action("funding", "confirm", payload)', block)
        funding = source[source.index("def funding_notices()"):source.index("def wallet_notices()")]
        self.assertIn("*DEPOSIT_FIELDS", funding, "the same txid field, so it is the same act")

    def test_what_a_deposit_needs_is_stated_rather_than_guessed(self) -> None:
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_instructions("):write.index("def _token_identity(")]
        for field in ("chain", "chain_id", "address", "destination_address",
                      "deposit_currency", "supported_tokens", "account_token_symbol",
                      "account_token_contract", "warnings"):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', block)

    def test_the_plugin_asks_polymarket_which_exact_contracts_are_accepted(self) -> None:
        """The platform exposes this fact; it must never be delegated back to the operator."""
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_instructions("):write.index("def _token_identity(")]
        self.assertIn("self._supported_assets", block)
        self.assertIn('"/supported-assets"', write)
        self.assertIn('"/deposit"', block)
        self.assertIn("supported_token_labels", block)
        self.assertNotIn("这里不替你猜", block)
        self.assertNotIn("先转一小笔", block)

    def test_a_deposit_is_followed_on_the_chain_not_taken_on_trust(self) -> None:
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_status("):write.index("def deposit_target(")]
        for state in ("not_found", "failed", "confirming", "arrived", "wrong_target", "wrong_token"):
            with self.subTest(state=state):
                self.assertIn(f'"{state}"', block)
        self.assertIn("eth_getTransactionReceipt", block)
        self.assertIn("eth_blockNumber", block)
        self.assertIn("self.bridge_status(", block)


class WhatThePolymarketDepositPanelActuallySaysTests(unittest.TestCase):
    """The displayed address and contracts come from the live bridge response shape."""

    def _transport(self):
        from prediction_market_agent.plugins.api._polymarket.config import PolymarketPluginConfig
        from prediction_market_agent.plugins.api._polymarket.write import PolymarketWriteTransport
        from tests._support import POLYMARKET_ENV

        settings = PolymarketPluginConfig.from_mapping(POLYMARKET_ENV)
        transport = PolymarketWriteTransport(settings)
        transport._setup_client = SimpleNamespace(
            environment=SimpleNamespace(collateral_token="0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"),
            wallet="0x1111111111111111111111111111111111111111",
        )
        transport._setup_client_at = time.time()
        transport._token_identity = lambda token: ("pUSD", 6)
        return transport

    def test_exact_polygon_usdc_contracts_and_bridge_address_are_returned(self) -> None:
        transport = self._transport()
        supported_reads = 0

        def bridge(method, path, *, payload=None):
            nonlocal supported_reads
            if path == "/deposit":
                self.assertEqual(payload["address"], "0x1111111111111111111111111111111111111111")
                return {"address": {"evm": "0x2222222222222222222222222222222222222222"}}
            supported_reads += 1
            return {"supportedAssets": [
                {"chainId": "137", "chainName": "Polygon", "token": {
                    "name": "USD Coin", "symbol": "USDC",
                    "address": "0x3333333333333333333333333333333333333333", "decimals": 6,
                }, "minCheckoutUsd": 2},
                {"chainId": "1", "chainName": "Ethereum", "token": {
                    "name": "USD Coin", "symbol": "USDC",
                    "address": "0x4444444444444444444444444444444444444444", "decimals": 6,
                }, "minCheckoutUsd": 3},
            ]}

        transport._bridge_json = bridge
        answer = transport.deposit_instructions()
        self.assertEqual(answer["address"], "0x2222222222222222222222222222222222222222")
        self.assertEqual(answer["supported_tokens"][0]["contract"],
                         "0x3333333333333333333333333333333333333333")
        labels = " ".join(answer["supported_token_labels"])
        self.assertIn("USD Coin / USDC", labels)
        self.assertIn("Polygon（chain id 137）", labels)
        self.assertNotIn("0x4444444444444444444444444444444444444444", labels)
        self.assertNotIn("猜", " ".join(answer["warnings"]))
        transport.withdrawal_options(all_chains=True)
        self.assertEqual(supported_reads, 1, "deposit and withdrawal must share one Bridge catalog")

    def test_balance_read_uses_the_non_trading_client(self) -> None:
        transport = self._transport()
        requested = []
        client = SimpleNamespace(
            get_balance_allowance=lambda **kwargs: SimpleNamespace(balance=2_880_000)
        )
        transport._require_client = lambda *, for_trading=True: (
            requested.append(for_trading) or client
        )
        self.assertEqual(transport.collateral_balance(), 2.88)
        self.assertEqual(requested, [False])

    def test_a_same_name_but_unsupported_contract_is_rejected(self) -> None:
        transport = self._transport()
        bridge_address = "0x2222222222222222222222222222222222222222"
        accepted = "0x3333333333333333333333333333333333333333"
        wrong = "0x5555555555555555555555555555555555555555"
        transport.deposit_instructions = lambda: {
            "address": bridge_address,
            "chain_id": 137,
            "supported_tokens": [{"symbol": "USDC", "contract": accepted,
                                  "decimals": 6, "minimum_usd": 2}],
            "supported_token_labels": [f"USDC · {accepted} · 最低 $2"],
        }
        recipient_topic = "0x" + "0" * 24 + bridge_address[2:]
        transport._rpc = lambda method, params: {
            "eth_getTransactionReceipt": {
                "status": "0x1", "blockNumber": "0x10", "logs": [{
                    "address": wrong,
                    "topics": [transport.TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
                    "data": hex(5_000_000),
                }],
            },
            "eth_blockNumber": "0x20",
        }.get(method)
        answer = transport.deposit_status("0x" + "a" * 64)
        self.assertEqual(answer["state"], "wrong_token")
        self.assertIn(wrong, answer["detail"])
        self.assertIn(accepted, answer["detail"])

    def test_arrived_means_the_bridge_route_completed_not_just_source_transfer(self) -> None:
        transport = self._transport()
        bridge_address = "0x2222222222222222222222222222222222222222"
        accepted = "0x3333333333333333333333333333333333333333"
        transport.deposit_instructions = lambda: {
            "address": bridge_address,
            "chain_id": 137,
            "supported_tokens": [{"symbol": "USDC", "contract": accepted,
                                  "decimals": 6, "minimum_usd": 2}],
            "supported_token_labels": [f"USDC · {accepted} · 最低 $2"],
        }
        recipient_topic = "0x" + "0" * 24 + bridge_address[2:]
        transport._rpc = lambda method, params: {
            "eth_getTransactionReceipt": {
                "status": "0x1", "blockNumber": "0x10", "logs": [{
                    "address": accepted,
                    "topics": [transport.TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
                    "data": hex(5_000_000),
                }],
            },
            "eth_blockNumber": "0x20",
        }.get(method)
        transport._bridge_json = lambda method, path, payload=None: {"transactions": [{
            "fromChainId": "137", "fromTokenAddress": accepted,
            "fromAmountBaseUnit": "5000000", "status": "COMPLETED",
        }]}
        answer = transport.deposit_status("0x" + "a" * 64)
        self.assertEqual(answer["state"], "arrived")
        self.assertEqual(answer["bridge_status"], "COMPLETED")


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
