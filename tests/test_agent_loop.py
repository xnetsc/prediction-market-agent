from ._support import *
from dataclasses import replace

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
    def test_missing_cli_backends_fall_through_to_compatible_api(self) -> None:
        from types import SimpleNamespace
        from prediction_market_agent.agent.decision import make_provider
        from prediction_market_agent.plugins.providers._shared import resolve_executable

        cfg = replace(config(Path("unused-state.json")),
                      decision_providers=("codex", "claude", "openai_compatible"))
        backend = FakeBackend("openai_compatible", [
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"},
            hold_decision(),
        ])
        attempted = []

        class Catalog:
            def get(self, kind, name):
                def factory(config):
                    attempted.append(name)
                    if name != "openai_compatible":
                        resolve_executable(f"/missing-client-installation/{name}")
                    return backend
                return SimpleNamespace(factory=factory)

        provider = make_provider(cfg, Catalog())
        self.assertEqual(attempted, ["codex", "claude", "openai_compatible"])
        self.assertEqual(set(provider.unavailable), {"codex", "claude"})
        self.assertEqual(provider.available_names, ("openai_compatible",))
        result = provider.decide({"market": {}}, tool_executor=lambda *_: {})
        self.assertEqual(result.provider, "openai_compatible")
        self.assertEqual(result.decision.action, "HOLD")

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


from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy


class FundingConversationTests(unittest.TestCase):
    """If a model does follow the funding instruction, the loop has to carry it through.

    This does not test the model's judgement - only a real model can be asked that. It tests the
    machinery around it: that the funding tools are offered, that their answers come back, and that
    a request still waiting for a person does not turn into a trade.
    """

    def _provider(self, script):
        from prediction_market_agent.agent.decision import AgentDecisionProvider

        backend = FakeBackend("fake", script)
        return backend, AgentDecisionProvider(backend, config(Path("/tmp/state.json")))

    def _payload(self):
        return {
            "market": {"title": "t", "outcome": "YES", "status": "OPEN"},
            "order_book": {"best_bid": 0.40, "best_ask": 0.42},
            "portfolio": {"platform": "p", "cash": 0.0, "equity": 0.0},
        }

    def test_the_funding_tools_are_offered_to_the_model(self) -> None:
        from prediction_market_agent.runtime.market_tools import DESCRIPTIONS

        for tool in ("ACCOUNT_FUNDS", "ENSURE_FUNDS", "FUNDING_STATUS"):
            with self.subTest(tool=tool):
                self.assertIn(tool, DESCRIPTIONS)

    def test_a_model_that_checks_and_asks_gets_both_answers_back(self) -> None:
        from prediction_market_agent.runtime.market_tools import DESCRIPTIONS

        seen = []

        def tools(name, arguments):
            seen.append((name, arguments))
            if name == "ACCOUNT_FUNDS":
                return {"available": 0.0, "currency": "USDT", "source": "platform"}
            return {"state": "pending", "request_id": "req-1", "available": 0.0}

        _, provider = self._provider([
            {"next_action": "ACCOUNT_FUNDS", "arguments_json": "{}", "reason": "check funds"},
            {"next_action": "ENSURE_FUNDS", "arguments_json": '{"amount": 50, "reason": "mispriced"}', "reason": "need funds"},
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"},
            hold_decision(),
        ])
        result = provider.decide(
            self._payload(), tool_executor=tools, tool_descriptions=DESCRIPTIONS,
            instructions=BuiltInDecisionStrategy().instructions,
        )
        self.assertEqual([name for name, _ in seen], ["ACCOUNT_FUNDS", "ENSURE_FUNDS"])
        self.assertEqual(seen[1][1]["reason"], "mispriced", "the reason reaches the plugin")
        self.assertEqual(result.decision.action, "HOLD")

    def test_the_tool_results_are_carried_into_the_next_turn(self) -> None:
        """A model that asked and was answered must be able to see the answer it got."""
        from prediction_market_agent.runtime.market_tools import DESCRIPTIONS

        backend, provider = self._provider([
            {"next_action": "ACCOUNT_FUNDS", "arguments_json": "{}", "reason": "check funds"},
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"},
            hold_decision(),
        ])
        captured = []
        original = backend.complete

        def recording(prompt, schema, schema_name):
            captured.append(prompt)
            return original(prompt, schema, schema_name)

        backend.complete = recording
        provider.decide(
            self._payload(),
            tool_executor=lambda n, a: {"available": 0.0, "source": "platform"},
            tool_descriptions=DESCRIPTIONS,
            instructions=BuiltInDecisionStrategy().instructions,
        )
        self.assertTrue(
            any('"available": 0.0' in text or "'available': 0.0" in text for text in captured[1:]),
            "the funding answer has to be visible when the model decides",
        )

    def test_the_instruction_reaches_the_model_with_the_tools(self) -> None:
        from prediction_market_agent.runtime.market_tools import DESCRIPTIONS

        backend, provider = self._provider([{"next_action": "DECIDE", "arguments_json": "{}", "reason": "ready"}, hold_decision()])
        captured = []
        original = backend.complete
        backend.complete = lambda p, s, n: (captured.append(p), original(p, s, n))[1]
        provider.decide(
            self._payload(), tool_executor=lambda n, a: {},
            tool_descriptions=DESCRIPTIONS,
            instructions=BuiltInDecisionStrategy().instructions,
        )
        self.assertIn("ENSURE_FUNDS", captured[0])
        self.assertIn("A pending request is not funding", captured[0])
