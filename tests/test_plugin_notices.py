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
