from ._support import *
from dataclasses import replace

from prediction_market_agent.agent.decision import (
    AGENT_LANGUAGES,
    AgentDecisionProvider,
    SYSTEM_INSTRUCTIONS,
    language_directive,
)


class Backend:
    name = "recording"

    def __init__(self, value):
        self.value = value
        self.prompts: list[str] = []

    def complete(self, prompt, schema, schema_name):
        self.prompts.append(prompt)
        return StructuredResult(value=self.value, raw_output="{}")


def hold() -> dict:
    return {
        "action": "HOLD", "order_type": "MARKET", "notional_usdt": 0, "quantity_fraction": 0,
        "limit_price": None, "confidence": 0.5, "estimated_probability": 0.5,
        "rationale": "nothing to do",
    }


class WhatTheDirectiveSaysTests(unittest.TestCase):
    """A rationale the operator cannot read is a rationale that was never given."""

    def test_chinese_asks_for_prose_only(self) -> None:
        text = language_directive("zh")
        self.assertIn("简体中文", text)
        for untouched in ("BUY", "SELL", "HOLD", "enum", "tool names"):
            self.assertIn(untouched, text, f"{untouched} must be named as something not to translate")

    def test_english_adds_nothing(self) -> None:
        """English is what the schemas and strategy already are; saying so again is noise."""
        self.assertEqual(language_directive("en"), "")

    def test_an_unknown_language_changes_nothing_rather_than_guessing(self) -> None:
        self.assertEqual(language_directive("fr"), "")
        self.assertEqual(language_directive(""), "")

    def test_a_market_is_quoted_in_its_own_words(self) -> None:
        """Resolution settles on the wording, not on a translation of it."""
        self.assertIn("in its own language", language_directive("zh"))


class WhereItReachesTests(unittest.TestCase):
    """One setting has to reach the decision, the round, the funding request and any question back."""

    def _provider(self, language: str, value):
        settings = replace(config(Path("/tmp/language-state.json")), agent_language=language)
        backend = Backend(value)
        return backend, AgentDecisionProvider(backend, settings)

    def test_it_is_in_the_preamble_every_prompt_shares(self) -> None:
        _, provider = self._provider("zh", hold())
        self.assertTrue(provider.preamble.startswith(SYSTEM_INSTRUCTIONS))
        self.assertIn("简体中文", provider.preamble)

    def test_the_final_decision_prompt_carries_it(self) -> None:
        backend, provider = self._provider("zh", hold())
        provider.decide({"market": {}}, tool_executor=None)
        self.assertIn("简体中文", backend.prompts[-1])

    def test_the_control_loop_carries_it_too(self) -> None:
        """The reason on each step is prose an operator reads while debugging."""
        script = [{"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"}, hold()]
        backend, provider = self._provider("zh", None)
        backend.complete = lambda prompt, schema, name: (
            backend.prompts.append(prompt),
            StructuredResult(value=script.pop(0), raw_output="{}"),
        )[1]
        provider.decide(
            {"market": {}},
            tool_executor=lambda name, arguments: {},
            tool_descriptions={"GET_TOPIC": {"purpose": "p", "arguments": {}}},
        )
        self.assertIn("简体中文", backend.prompts[0])

    def test_a_discovery_round_carries_it(self) -> None:
        """Selections, the skip reason and the pacing reason are all read by a person."""
        backend, provider = self._provider("zh", {"ok": True})
        provider.run(
            {"candidates": []},
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            schema_name="market_discovery", mission="Pick markets.",
        )
        self.assertIn("简体中文", backend.prompts[-1])

    def test_english_leaves_the_prompt_as_it_was(self) -> None:
        backend, provider = self._provider("en", hold())
        provider.decide({"market": {}}, tool_executor=None)
        self.assertNotIn("简体中文", backend.prompts[-1])


class TheSettingTests(unittest.TestCase):
    def test_it_is_offered_with_both_languages(self) -> None:
        from prediction_market_agent.core.config import APPLICATION_FIELDS

        field = next(item for item in APPLICATION_FIELDS if item.name == "agent_language")
        self.assertEqual(set(field.options), set(AGENT_LANGUAGES))
        self.assertIn(field.default, AGENT_LANGUAGES)

    def test_the_default_matches_the_console_the_operator_reads(self) -> None:
        self.assertEqual(Config().agent_language, "zh")


class TheBrowserChoosesUntilSomebodyDoesTests(unittest.TestCase):
    """The server cannot see a browser; the page can, and it is that browser showing the reasoning."""

    def test_the_default_is_taken_from_the_browser_and_never_overrides_a_choice(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; this check needs a JavaScript engine")
        completed = subprocess.run(
            [node, "tests/language_default_check.js",
             "src/prediction_market_agent/runtime/static/dashboard-views.js"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_the_language_can_be_changed_where_the_reasoning_is_read(self) -> None:
        shell = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        self.assertIn('id="ledgerLanguage"', shell)
        self.assertIn("saveLedgerLanguage(this.value)", shell)
