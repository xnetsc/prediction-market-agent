from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.agent.decision_evaluator import DecisionEvaluatorError, DecisionEvaluatorPool
from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery
from prediction_market_agent.agent.research import ResearchToolbox
from prediction_market_agent.plugins.evaluators.jev import SchemaDecisionEvaluator
from prediction_market_agent.runtime.evaluator_tool import EvaluatorToolContribution, TOOL_NAME
from prediction_market_agent.runtime.market_discovery import _DiscoveryToolbox
from prediction_market_agent.runtime.memory import SessionMemory


FACT_CHECK = {
    "state": {"bid": 0.40, "ask": 0.43, "source": "verified order book"},
    "questions": {
        "spread_gate": {
            "type": "choice",
            "instructions": "Compare the supplied spread against the stated threshold.",
            "criteria": {"PASS": "Spread at most 0.05", "FAIL": "Spread above 0.05"},
        }
    },
}


class _Evaluator:
    def __init__(self, name: str, *, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = 0
        self.state_kinds = ("text", "json")

    def answer_questions(self, state, questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("temporary evaluator failure")
        return {"answers": {"spread_gate": {"type": "choice", "choice": "PASS",
                "confidence": 0.9, "probabilities": {"PASS": 0.9, "FAIL": 0.1}}},
                "model": self.name, "usage": {}}


class EvaluatorToolTests(unittest.TestCase):
    def test_tool_describes_scope_and_falls_back_in_configured_order(self):
        first, second = _Evaluator("first", fail=True), _Evaluator("second")
        pool = DecisionEvaluatorPool([first, second], order_mode="CONFIGURED")
        tool = EvaluatorToolContribution(pool)
        purpose = tool.descriptions[TOOL_NAME]["purpose"]
        self.assertIn("if/else", purpose)
        self.assertIn("state", purpose)
        self.assertIn("BUY/SELL", purpose)
        result = tool.execute(TOOL_NAME, FACT_CHECK)
        self.assertEqual((first.calls, second.calls), (1, 1))
        self.assertEqual(result["evaluator_name"], "second")
        self.assertEqual(result["answers"]["spread_gate"]["choice"], "PASS")
        self.assertTrue(result["advisory_only"])

    def test_rejects_unbounded_or_nonfactual_protocols_before_calling_model(self):
        evaluator = _Evaluator("only")
        tool = EvaluatorToolContribution(DecisionEvaluatorPool([evaluator]))
        for arguments in (
            {"state": {}, "questions": FACT_CHECK["questions"]},
            {"state": FACT_CHECK["state"], "questions": {}},
            {"state": FACT_CHECK["state"], "questions": {"trade": {
                "type": "trade", "instructions": "Buy?", "criteria": {"YES": "yes"}}}},
            {"state": {"ask": float("nan")}, "questions": FACT_CHECK["questions"]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                tool.execute(TOOL_NAME, arguments)
        self.assertEqual(evaluator.calls, 0)

    def test_invalid_typed_output_falls_back_without_exposing_it_to_agent(self):
        invalid = _Evaluator("invalid")
        valid = _Evaluator("valid")
        invalid.answer_questions = lambda _state, _questions: {"answers": {
            "spread_gate": {"type": "choice", "choice": "PASS", "confidence": 1.5,
                            "probabilities": {"PASS": 1.0}}}}
        pool = DecisionEvaluatorPool([invalid, valid], order_mode="CONFIGURED")
        result = EvaluatorToolContribution(pool).execute(TOOL_NAME, FACT_CHECK)
        self.assertEqual(result["evaluator_name"], "valid")
        self.assertIn("invalid confidence", pool.fallback_errors["invalid"])

    def test_score_and_noul_answers_must_stay_inside_declared_ranges(self):
        cases = (
            ({"type": "score", "instructions": "Rate the stated fact.",
              "criteria": ["low", "high"]}, {"type": "score", "score": 2,
                                           "confidence": 0.9}),
            ({"type": "noul", "instructions": "Is the stated fact true?",
              "criteria": {"true": "yes", "false": "no"}},
             {"type": "noul", "noul": True, "confidence": 0.9}),
        )
        for question, answer in cases:
            with self.subTest(kind=question["type"]):
                evaluator = _Evaluator("invalid")
                evaluator.answer_questions = lambda _state, _questions, item=answer: {
                    "answers": {"check": item}}
                pool = DecisionEvaluatorPool([evaluator])
                with self.assertRaises(DecisionEvaluatorError):
                    pool.answer_questions(FACT_CHECK["state"], {"check": question})

    def test_same_tool_is_available_to_discovery_and_trade_agent(self):
        contribution = EvaluatorToolContribution(DecisionEvaluatorPool([_Evaluator("cheap")]))
        trade = ResearchToolbox([contribution], None)
        self.assertIn(TOOL_NAME, trade.descriptions)
        self.assertTrue(trade.execute(TOOL_NAME, FACT_CHECK)["ok"])
        with tempfile.TemporaryDirectory() as directory:
            memory = SessionMemory(Path(directory) / "sessions.sqlite3")
            discovery = _DiscoveryToolbox(
                plugin=object(), memory=memory, platform="venue",
                budget=BuiltInMarketDiscovery().budget(), cross_platform_search=None,
                research=[contribution],
            )
            self.assertIn(TOOL_NAME, discovery.descriptions)
            self.assertTrue(discovery.execute(TOOL_NAME, FACT_CHECK)["ok"])
            memory.close()

    def test_schema_evaluator_reuses_state_questions_adapter(self):
        evaluator = object.__new__(SchemaDecisionEvaluator)
        with patch.object(SchemaDecisionEvaluator, "_evaluate", return_value={"answers": {}}) as call:
            evaluator.answer_questions(FACT_CHECK["state"], FACT_CHECK["questions"])
        self.assertEqual(call.call_args.args[0],
                         {"workflow": "agent_fact_check", "facts": FACT_CHECK["state"]})
        self.assertEqual(call.call_args.args[1], FACT_CHECK["questions"])

    def test_multimodal_state_uses_only_an_image_capable_evaluator(self):
        text = _Evaluator("text")
        vision = _Evaluator("vision")
        vision.state_kinds = ("text", "json", "image", "multimodal")
        state = {"type": "multimodal", "text": "chart", "images": [{
            "type": "image", "media_type": "image/png",
            "data": base64.b64encode(b"tiny-image").decode(),
        }]}
        result = EvaluatorToolContribution(
            DecisionEvaluatorPool([text, vision], order_mode="CONFIGURED")
        ).execute(TOOL_NAME, {"state": state, "questions": FACT_CHECK["questions"]})
        self.assertEqual((text.calls, vision.calls), (0, 1))
        self.assertEqual(result["evaluator_name"], "vision")

    def test_multimodal_state_rejects_remote_or_invalid_image_data(self):
        tool = EvaluatorToolContribution(DecisionEvaluatorPool([_Evaluator("text")]))
        for image in (
            {"type": "image_url", "url": "https://example.test/chart.png"},
            {"type": "image", "media_type": "image/png", "data": "not base64"},
        ):
            with self.subTest(image=image), self.assertRaises(ValueError):
                tool.execute(TOOL_NAME, {"state": {
                    "type": "multimodal", "text": "chart", "images": [image],
                }, "questions": FACT_CHECK["questions"]})
