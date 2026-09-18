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
        for field in ("chain", "chain_id", "address", "token_symbol", "token_contract", "warnings"):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', block)

    def test_a_deposit_is_followed_on_the_chain_not_taken_on_trust(self) -> None:
        write = Path("src/prediction_market_agent/plugins/api/_polymarket/write.py").read_text()
        block = write[write.index("def deposit_status("):write.index("def deposit_target(")]
        for state in ("not_found", "failed", "confirming", "credited", "wrong_target"):
            with self.subTest(state=state):
                self.assertIn(f'"{state}"', block)
        self.assertIn("eth_getTransactionReceipt", block)
        self.assertIn("eth_blockNumber", block)
