from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from prediction_market_agent.agent.decision import AgentRunResult
from prediction_market_agent.agent.decision_evaluator import (
    CandidateAssessment,
    ContinuationAssessment,
    DecisionEvaluatorError,
    DecisionEvaluatorPool,
)
from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery
from prediction_market_agent.plugin_system.contracts import Market, Outcome, Topic, TopicDetail
from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.evaluators.jev import (
    SchemaDecisionEvaluator,
    initialize_plugin,
    openrouter_evaluator_model_choices,
)
from prediction_market_agent.runtime.evaluation import MarketEvaluationMixin
from prediction_market_agent.runtime.market_discovery import DiscoveryEngine, REVIEW_SETTLE_MS
from prediction_market_agent.runtime.memory import SessionMemory

from tests.test_market_discovery import FakePlugin


class _JevHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    malformed = False
    reject_schema = False
    ignore_schema = False

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        if self.__class__.reject_schema and "response_format" in body:
            payload = b'{"error":{"message":"response_format is not supported"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.__class__.malformed or (
            self.__class__.ignore_schema and "response_format" in body
        ):
            payload = json.dumps({
                "choices": [{"message": {"content": "not-json"}}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if "state" in body:
            protocol_input = body
        else:
            protocol_input = json.loads(body["messages"][-1]["content"])
        workflow = protocol_input["state"]["workflow"]
        answers = {}
        for name, question in protocol_input["questions"].items():
            if question["type"] == "score":
                answers[name] = {"type": "score", "score": 3.2, "confidence": 0.8}
            elif question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.25}
            else:
                choice = "PRIORITIZE"
                if name == "next_action":
                    choice = "CONTINUE_DISCOVERY"
                answers[name] = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 0.75,
                    "probabilities": {choice: 1.0},
                }
        result = {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        if "state" not in body:
            message = {"content": json.dumps({"answers": answers})}
            if "tools" in body:
                message = {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_typed_answers",
                                "arguments": json.dumps({"answers": answers}),
                            },
                        }
                    ],
                }
            result = {"model": "private-jev", "choices": [{"message": message}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        payload = json.dumps({"data": [
            {
                "id": "vendor/schema-chat",
                "name": "Schema Chat",
                "supported_parameters": ["structured_outputs"],
            },
            {
                "id": "vendor/plain-chat",
                "name": "Plain Chat",
                "supported_parameters": [],
            },
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


class OpenRouterJevAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        _JevHandler.requests = []
        _JevHandler.malformed = False
        _JevHandler.reject_schema = False
        _JevHandler.ignore_schema = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _JevHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_real_http_contract_uses_all_three_typed_primitives(self) -> None:
        evaluator = SchemaDecisionEvaluator(
            "secret", "~typesafe/jev-latest", "", 2, 10,
            endpoint=f"http://127.0.0.1:{self.server.server_port}/api/alpha/decisions",
        )
        candidates = evaluator.evaluate_candidates(
            {"cycle": 1}, [{"candidate_id": "m1", "question": "Will it happen?"}]
        )
        continuation = evaluator.assess_continuation({}, [], {"has_more": True})

        self.assertEqual(candidates[0].action, "NEEDS_DATA")
        self.assertAlmostEqual(candidates[0].quality, 0.8)
        self.assertEqual(continuation.action, "CONTINUE_DISCOVERY")
        question_types = {
            question["type"]
            for request in _JevHandler.requests
            for question in request["body"]["questions"].values()
        }
        self.assertEqual(question_types, {"choice", "score", "noul"})
        self.assertTrue(all(item["authorization"] == "Bearer secret" for item in _JevHandler.requests))
        self.assertTrue(all(
            item["path"] == "/api/alpha/decisions" for item in _JevHandler.requests
        ))
        self.assertTrue(all(
            item["body"]["model"] == "~typesafe/jev-latest" for item in _JevHandler.requests
        ))

    def test_plugin_reuses_only_the_shared_key_and_keeps_its_own_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "config" / "plugins"
            configs.mkdir(parents=True)
            (configs / "openrouter.json").write_text(json.dumps({
                "OPENROUTER_API_KEY": "shared-key",
                "OPENROUTER_HTTP_PROXY": "http://shared-proxy.invalid:8080",
            }))
            (configs / "jev.json").write_text(json.dumps({
                "JEV_CONNECTION": "OPENROUTER",
                "JEV_SHARED_PROVIDER": "openrouter",
                "JEV_MODEL": "~typesafe/jev-latest",
                "JEV_CUSTOM_BASE_URL": "",
                "JEV_CUSTOM_MODEL": "",
                "JEV_HTTP_PROXY": "DIRECT",
                "JEV_TIMEOUT_SECONDS": 30,
                "JEV_BATCH_SIZE": 32,
            }))
            spec = initialize_plugin(PluginInitializationContext(
                kind="decision_evaluator", module_path=Path("jev.py"),
                working_directory=root, shared_http_proxy="DIRECT",
            ))

            evaluator = spec.factory(None)

            self.assertEqual(evaluator.api_key, "shared-key")
            self.assertEqual(evaluator.proxy, "")
            self.assertTrue(spec.readiness().ready)

    def test_custom_connection_accepts_an_optional_key_and_uses_custom_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "config" / "plugins"
            configs.mkdir(parents=True)
            (configs / "jev.json").write_text(json.dumps({
                "JEV_CONNECTION": "CUSTOM",
                "JEV_API_KEY": "",
                "JEV_SHARED_PROVIDER": "openrouter",
                "JEV_MODEL": "~typesafe/jev-latest",
                "JEV_CUSTOM_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/custom/v1",
                "JEV_CUSTOM_MODEL": "private-jev",
                "JEV_HTTP_PROXY": "DIRECT",
                "JEV_TIMEOUT_SECONDS": 30,
                "JEV_BATCH_SIZE": 32,
            }))
            spec = initialize_plugin(PluginInitializationContext(
                kind="decision_evaluator", module_path=Path("jev.py"),
                working_directory=root, shared_http_proxy="DIRECT",
            ))

            evaluator = spec.factory(None)
            evaluator.assess_continuation({}, [], {"has_more": True})

            self.assertEqual(evaluator.api_key, "")
            self.assertEqual(evaluator.model, "private-jev")
            self.assertEqual(_JevHandler.requests[-1]["path"], "/custom/v1/chat/completions")
            self.assertIsNone(_JevHandler.requests[-1]["authorization"])
            self.assertTrue(
                _JevHandler.requests[-1]["body"]["response_format"]["json_schema"]["strict"]
            )
            self.assertTrue(spec.readiness().ready)

    def test_custom_chat_prose_is_rejected_instead_of_becoming_a_decision(self) -> None:
        _JevHandler.malformed = True
        evaluator = SchemaDecisionEvaluator(
            "", "chat-model", "", 2, 10,
            endpoint=f"http://127.0.0.1:{self.server.server_port}/chat/completions",
            connection_name="custom", protocol="chat_json",
        )

        with self.assertRaises(DecisionEvaluatorError):
            evaluator.assess_continuation({}, [], {"has_more": True})

    def test_custom_connection_prefers_native_schema_then_falls_back_to_forced_tool(self) -> None:
        _JevHandler.reject_schema = True
        evaluator = SchemaDecisionEvaluator(
            "", "chat-model", "", 2, 10,
            endpoint=f"http://127.0.0.1:{self.server.server_port}/chat/completions",
            connection_name="custom", protocol="chat_auto",
        )

        result = evaluator.assess_continuation({}, [], {"has_more": True})

        self.assertEqual(result.action, "CONTINUE_DISCOVERY")
        self.assertEqual(len(_JevHandler.requests), 2)
        self.assertIn("response_format", _JevHandler.requests[0]["body"])
        fallback = _JevHandler.requests[1]["body"]
        self.assertEqual(
            fallback["tools"][0]["function"]["name"], "submit_typed_answers"
        )
        self.assertEqual(
            fallback["tool_choice"]["function"]["name"], "submit_typed_answers"
        )

    def test_custom_connection_falls_back_when_schema_is_silently_ignored(self) -> None:
        _JevHandler.ignore_schema = True
        evaluator = SchemaDecisionEvaluator(
            "", "chat-model", "", 2, 10,
            endpoint=f"http://127.0.0.1:{self.server.server_port}/chat/completions",
            connection_name="custom", protocol="chat_auto",
        )

        result = evaluator.assess_continuation({}, [], {"has_more": True})

        self.assertEqual(result.action, "CONTINUE_DISCOVERY")
        self.assertEqual(len(_JevHandler.requests), 2)
        self.assertIn("response_format", _JevHandler.requests[0]["body"])
        self.assertIn("tools", _JevHandler.requests[1]["body"])

    def test_openrouter_chat_models_are_filtered_and_schema_constrained(self) -> None:
        choices = openrouter_evaluator_model_choices(
            {"OPENROUTER_API_KEY": "secret"},
            models_url=f"http://127.0.0.1:{self.server.server_port}/models",
            proxy_settings={"proxy": ""},
        )
        values = {item["value"] for item in choices}
        self.assertIn("~typesafe/jev-latest", values)
        self.assertIn("vendor/schema-chat", values)
        self.assertNotIn("vendor/plain-chat", values)

        evaluator = SchemaDecisionEvaluator(
            "secret", "vendor/schema-chat", "", 2, 10,
            endpoint=f"http://127.0.0.1:{self.server.server_port}/chat/completions",
            connection_name="openrouter", protocol="chat_json", require_parameters=True,
        )
        evaluator.assess_continuation({}, [], {"has_more": True})
        body = _JevHandler.requests[-1]["body"]
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["provider"], {"require_parameters": True})


class _AdaptiveEvaluator:
    available = True
    errors: dict[str, str] = {}

    def evaluate_candidates(self, _state, candidates):
        return [
            CandidateAssessment(
                candidate_id=item["candidate_id"], action="PRIORITIZE", quality=0.9,
                provider="replaceable-test-evaluator",
            )
            for item in candidates
        ]

    def assess_continuation(self, _state, _frontier, _page):
        return ContinuationAssessment(
            action="CONTINUE_DISCOVERY", marginal_value=0.8,
            provider="replaceable-test-evaluator",
        )


class _PauseThenContinueEvaluator(_AdaptiveEvaluator):
    def __init__(self) -> None:
        self.calls = 0

    def assess_continuation(self, _state, _frontier, _page):
        self.calls += 1
        return ContinuationAssessment(
            action="PAUSE_AND_RESUME" if self.calls <= 2 else "CONTINUE_DISCOVERY",
            marginal_value=0.5,
            provider="replaceable-test-evaluator",
        )


class _HighConfidenceCoarseEvaluator(_AdaptiveEvaluator):
    def evaluate_candidates(self, _state, candidates):
        return [
            CandidateAssessment(
                candidate_id=item["candidate_id"], action="REJECT", quality=0.1,
                confidence=0.95, provider="replaceable-test-evaluator",
            )
            for item in candidates
        ]

    def assess_continuation(self, _state, _frontier, _page):
        return ContinuationAssessment(
            action="CONTINUE_DISCOVERY", marginal_value=0.8, confidence=0.95,
            provider="replaceable-test-evaluator",
        )


class _OffsetRecordingPlugin(FakePlugin):
    def __init__(self, topics: int) -> None:
        super().__init__(topics)
        self.offsets: list[int] = []

    def list_topics(self, *, offset: int, limit: int):
        self.offsets.append(offset)
        return super().list_topics(offset=offset, limit=limit)


class _AdaptiveProvider:
    name = "structured-provider"

    def __init__(self) -> None:
        self.continuations = 0
        self.final_candidate_count = 0

    def run(self, payload, **options):
        if options.get("schema_name") == "discovery_continuation":
            self.continuations += 1
            return AgentRunResult(
                value={"action": "CONTINUE_DISCOVERY", "reason": "source has another page"},
                raw_output="{}", provider=self.name, research_trace=[],
            )
        self.final_candidate_count = len(payload["candidates"])
        return AgentRunResult(
            value={
                "selections": [
                    {"topic_id": item["topic_id"], "reason": "typed priority", "priors": []}
                    for item in payload["candidates"][:15]
                ],
                "skipped_reason": "", "next_scan_seconds": 0, "next_survey_queries": [],
                "pacing_reason": "complete source", "headline": "selected typed frontier",
            },
            raw_output="{}", provider=self.name, research_trace=[],
        )


class AdaptiveDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def test_typed_evaluator_can_scan_past_200_and_select_past_10(self) -> None:
        plugin = FakePlugin(260)
        provider = _AdaptiveProvider()
        engine = DiscoveryEngine(
            memory=self.memory,
            strategy=BuiltInMarketDiscovery(),
            provider=provider,
            evaluator=_AdaptiveEvaluator(),
            max_scan_seconds=30,
            max_scan_pages=20,
            evolution_enabled=True,
        )
        selected = engine.discover(platform="fake", plugin=plugin)
        observed = self.memory.connection.execute("SELECT COUNT(*) FROM topic_observations").fetchone()[0]

        self.assertEqual(observed, 260)
        self.assertEqual(len(selected), 15)
        self.assertGreater(plugin.list_calls, 8)
        self.assertGreater(provider.continuations, 8)
        self.assertIn("typed_evaluation", json.loads(
            self.memory.connection.execute(
                "SELECT features_json FROM discovery_selections LIMIT 1"
            ).fetchone()[0]
        ))

    def test_pause_persists_cursor_and_next_batch_resumes(self) -> None:
        plugin = _OffsetRecordingPlugin(120)
        evaluator = _PauseThenContinueEvaluator()
        engine = DiscoveryEngine(
            memory=self.memory,
            strategy=BuiltInMarketDiscovery(),
            provider=_AdaptiveProvider(),
            evaluator=evaluator,
            max_scan_seconds=30,
            max_scan_pages=20,
            evolution_enabled=True,
        )

        engine.discover(platform="fake", plugin=plugin)
        first_resume = self.memory.survey_plan("fake")["resume"]
        engine.discover(platform="fake", plugin=plugin)

        self.assertEqual(first_resume["cursors"]["catalog"], 50)
        self.assertEqual(plugin.offsets[:3], [0, 25, 50])
        self.assertEqual(self.memory.survey_plan("fake")["resume"], {})

    def test_high_confidence_coarse_screen_reduces_tokens_but_keeps_audit_sample(self) -> None:
        plugin = FakePlugin(100)
        provider = _AdaptiveProvider()
        engine = DiscoveryEngine(
            memory=self.memory,
            strategy=BuiltInMarketDiscovery(),
            provider=provider,
            evaluator=_HighConfidenceCoarseEvaluator(),
            max_scan_seconds=30,
            max_scan_pages=20,
            evolution_enabled=True,
        )

        selected = engine.discover(platform="fake", plugin=plugin)

        self.assertEqual(provider.continuations, 0)
        self.assertEqual(provider.final_candidate_count, 10)
        self.assertEqual(len(selected), 10)
        context = json.loads(self.memory.connection.execute(
            "SELECT context_json FROM decision_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        screening = context["typed_frontier_filter"]
        self.assertEqual(screening["confidence_threshold"], 0.9)
        self.assertEqual(len(screening["restored_for_quality_audit"]), 10)
        self.assertEqual(screening["excluded_count"], 90)

    def test_screening_threshold_adapts_from_point_nine_without_crossing_point_eight(self) -> None:
        engine = object.__new__(DiscoveryEngine)
        engine.memory = SimpleNamespace(reviewed_selection_outcomes=lambda: [])
        self.assertEqual(engine._adaptive_screening_threshold(), 0.9)

        def reviewed(useful: bool):
            return [
                {
                    "features": {"typed_evaluation": {
                        "action": "REJECT", "confidence": 0.95,
                    }},
                    "outcome": {"useful": useful},
                }
                for _ in range(100)
            ]

        engine.memory = SimpleNamespace(reviewed_selection_outcomes=lambda: reviewed(False))
        lower = engine._adaptive_screening_threshold()
        engine.memory = SimpleNamespace(reviewed_selection_outcomes=lambda: reviewed(True))
        higher = engine._adaptive_screening_threshold()

        self.assertGreaterEqual(lower, 0.8)
        self.assertLess(lower, 0.9)
        self.assertGreater(higher, 0.9)


class CrossTopicSchedulingTests(unittest.TestCase):
    @staticmethod
    def _detail(topic: Topic) -> TopicDetail:
        outcomes = tuple(Outcome(f"{topic.topic_id}-{index}", f"O{index}", 0.5) for index in range(4))
        return TopicDetail(
            topic=topic, start_time_ms=0, end_time_ms=int(time.time() * 1000) + 60_000,
            fee_bps=0, markets=(Market(f"m-{topic.topic_id}", "M", "Q", "OPEN", 1, 1, outcomes),),
        )

    def test_queue_round_robins_topics_and_is_not_cut_at_six(self) -> None:
        topics = [Topic(str(index), f"T{index}", "Q", "D", "c", "OPEN", 1, 1) for index in range(3)]
        details = {topic.topic_id: self._detail(topic) for topic in topics}
        engine = SimpleNamespace(
            decision_strategy=SimpleNamespace(select_markets=list, select_outcomes=list),
            _settle_if_possible=lambda *_args: None,
        )
        runtime = SimpleNamespace(plugin=SimpleNamespace(get_topic=lambda topic_id: details[topic_id]))

        queue = MarketEvaluationMixin._plan_outcomes(engine, runtime, topics)

        self.assertEqual([item[0].topic_id for item in queue[:6]], ["0", "1", "2", "0", "1", "2"])
        self.assertEqual(len(queue), 12)

    def test_failed_book_read_is_not_a_decision_attempt(self) -> None:
        topic = Topic("1", "T", "Q", "D", "c", "OPEN", 1, 1)
        detail = self._detail(topic)
        engine = SimpleNamespace(_decisions_this_cycle=0, _max_decisions_this_cycle=100)
        runtime = SimpleNamespace(plugin=SimpleNamespace(
            name="fake", get_order_book=lambda *_args: (_ for _ in ()).throw(RuntimeError("down"))
        ))

        with self.assertRaises(RuntimeError):
            MarketEvaluationMixin._evaluate_outcome(
                engine, runtime, topic, detail, detail.markets[0], detail.markets[0].outcomes[0], 60
            )
        self.assertEqual(engine._decisions_this_cycle, 0)


class DiscoveryAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = SessionMemory(Path(self.temp.name) / "session.sqlite3")
        self.addCleanup(self.memory.close)

    def _selection(self, topic_id: str) -> int:
        return self.memory.record_discovery_selection(
            platform="fake", strategy="s", market_topic_id=topic_id, position=1,
            reason="r", priors=[], features={"buckets": ["test"]},
        )

    def _decision(self, selection_id: int, topic_id: str, *, filled: bool) -> None:
        decision_id = self.memory.begin_decision(
            platform="fake", market_topic_id=topic_id, market_id="m", token_id="yes",
            strategy_name="s", strategy_sha256="x", context={},
            discovery_selection_id=selection_id,
        )
        self.memory.complete_decision(
            decision_id, proposed_decision={"action": "BUY", "notional_usdt": 10},
            final_decision={"action": "BUY", "notional_usdt": 10},
            execution={"status": "FILLED"} if filled else None,
            status="EXECUTED" if filled else "RISK_REJECTED",
        )

    def test_proposal_is_not_activity_and_fill_is_attributed_by_selection(self) -> None:
        proposed = self._selection("proposal")
        filled = self._selection("filled")
        self._decision(proposed, "proposal", filled=False)
        self._decision(filled, "filled", filled=True)

        proposal_result = self.memory.selection_downstream(
            platform="fake", market_topic_id="proposal", since_ms=0,
            until_ms=int(time.time() * 1000) + 1000, selection_id=proposed,
        )
        fill_result = self.memory.selection_downstream(
            platform="fake", market_topic_id="filled", since_ms=0,
            until_ms=int(time.time() * 1000) + 1000, selection_id=filled,
        )

        self.assertFalse(proposal_result["useful"])
        self.assertEqual(proposal_result["filled_orders"], 0)
        self.assertTrue(fill_result["useful"])
        self.assertEqual(fill_result["buy_fills"], 1)

    def test_nonfinal_review_is_revisited_until_resolution_supplies_pnl(self) -> None:
        selection = self._selection("resolved")
        self._decision(selection, "resolved", filled=True)
        now = int(time.time() * 1000)
        interim = self.memory.selection_downstream(
            platform="fake", market_topic_id="resolved", since_ms=0,
            until_ms=now + 1000, selection_id=selection,
        )
        self.memory.mark_selection_reviewed(selection, interim, final=False)
        pending = self.memory.unreviewed_selections(
            settled_before_ms=now + REVIEW_SETTLE_MS + 1000
        )
        self.assertIn(selection, {item["id"] for item in pending})

        self.memory.record_action(
            platform="fake", market_topic_id="resolved", token_id="yes", action="REDEEM",
            request={}, result={"realized_pnl": 2.5},
        )
        final = self.memory.selection_downstream(
            platform="fake", market_topic_id="resolved", since_ms=0,
            until_ms=int(time.time() * 1000) + 1000, selection_id=selection,
        )
        self.assertTrue(final["outcome_complete"])
        self.assertTrue(final["net_pnl_known"])
        self.assertEqual(final["realized_pnl"], 2.5)


class ConfidenceThatStandsApartTests(unittest.TestCase):
    """A timid screener's 0.6, in a round of 0.1s, says something a fixed floor cannot hear.

    The floor stays: sure in absolute terms is still sure. Beside it there is now a second way to
    be trusted - standing clearly above this round's own answers - and which answers those are is
    asked of the screener that produced them, because a constant written here would be a number
    nobody measured applied to every model and every batch alike.
    """

    class Screener:
        name = "jev"
        available = True

        def __init__(self, verdicts=None, fails=False):
            self.verdicts = verdicts or {}
            self.fails = fails
            self.asked = []

        def screen_outliers(self, state, assessments):
            if self.fails:
                raise RuntimeError("screener is down")
            self.asked.append({"state": state, "assessments": assessments})
            return dict(self.verdicts)

    def _pool(self, *screeners):
        return DecisionEvaluatorPool(list(screeners))

    def test_only_the_answers_below_the_floor_are_put_back_to_the_screener(self) -> None:
        screener = self.Screener({"a": True})
        pool = self._pool(screener)
        assessments = [
            {"candidate_id": "a", "confidence": 0.6, "under_review": True},
            {"candidate_id": "b", "confidence": 0.12, "under_review": True},
            {"candidate_id": "c", "confidence": 0.95, "under_review": False},
            {"candidate_id": "d", "confidence": 0.1, "under_review": True},
        ]
        self.assertEqual(pool.screen_outliers({}, assessments), {"a": True})
        sent = screener.asked[0]["assessments"]
        self.assertEqual(len(sent), 4, "the whole round is supplied; separation is about the batch")

    def test_a_batch_too_small_to_have_a_crowd_is_not_asked_about(self) -> None:
        screener = self.Screener({"a": True})
        pool = self._pool(screener)
        self.assertEqual(pool.screen_outliers({}, [{"candidate_id": "a", "under_review": True}]), {})
        self.assertEqual(screener.asked, [], "nothing was asked")

    def test_screeners_that_looked_and_disagreed_outvote_one_that_did_not(self) -> None:
        """This path drops candidates, so one opinion against two is not enough to act on."""
        pool = self._pool(
            self.Screener({"a": True}), self.Screener({"a": False}), self.Screener({"a": False})
        )
        assessments = [{"candidate_id": key, "under_review": True} for key in "abcd"]
        self.assertEqual(pool.screen_outliers({}, assessments), {"a": False})

    def test_a_screener_that_fails_is_recorded_and_not_counted_as_agreement(self) -> None:
        working = self.Screener({"a": True})
        pool = self._pool(working, self.Screener(fails=True))
        assessments = [{"candidate_id": key, "under_review": True} for key in "abcd"]
        self.assertEqual(pool.screen_outliers({}, assessments), {"a": True})
        self.assertIn("jev", pool.errors)

    def test_the_gate_accepts_either_route(self) -> None:
        from prediction_market_agent.agent.decision_evaluator import is_high_confidence

        self.assertTrue(is_high_confidence(0.95, 0.9), "the floor still passes what is above it")
        self.assertFalse(is_high_confidence(0.6, 0.9), "and still refuses what is below it")
        source = Path("src/prediction_market_agent/runtime/market_discovery.py").read_text()
        gate = source[source.index("assessment.action in {\"DEFER\", \"REJECT\"}"):][:400]
        self.assertIn("is_high_confidence", gate)
        self.assertIn("trusted_outliers", gate)


class TheScreenerRemembersTests(unittest.TestCase):
    """The coarse screener judged every candidate as if it had never seen it.

    It runs on hundreds of markets a round, and with only the candidate in front of it, a market
    screened and decided a dozen times is screened again on the same facts and passed on again -
    so the expensive round behind it keeps re-deciding what is already settled, and keeps reaching
    the same answer. What it was missing is its own record: how it called this market before, what
    the deciding model concluded, and how its verdicts have turned out on this platform at all.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = SessionMemory(Path(self.directory.name) / "s.sqlite3")
        self.addCleanup(self.memory.connection.close)

    def _screened(self, topic_id: str, action: str, confidence: float) -> None:
        self.memory.record_topic_observations(platform="poly", observations=[{
            "market_topic_id": topic_id, "title": "T", "status": "OPEN",
            "liquidity_usdt": 1.0, "volume_usdt": 1.0, "end_time_ms": None,
            "reference_price": None,
            "features": {"typed_evaluation": {"action": action, "confidence": confidence}},
        }])

    def _decided(self, topic_id: str, action: str, headline: str, revisit: str) -> None:
        decision_id = self.memory.begin_decision(
            platform="poly", market_topic_id=topic_id, market_id="m", token_id="o",
            strategy_name="general_agent", strategy_sha256="x", context={},
        )
        self.memory.complete_decision(
            decision_id, provider="p", status="NO_ACTION",
            final_decision={"action": action, "headline": headline, "revisit_when": revisit},
        )

    def test_every_screening_and_every_decision_is_kept_not_only_the_last(self) -> None:
        for action in ("PRIORITIZE", "NEEDS_DATA", "PRIORITIZE"):
            self._screened("t1", action, 0.7)
        self._decided("t1", "HOLD", "观望一", "跌破 0.40 再问")
        self._decided("t1", "HOLD", "观望二", "跌破 0.40 再问")
        history = self.memory.screening_history(platform="poly")["t1"]
        self.assertEqual(history["screen_counts"], {"PRIORITIZE": 2, "NEEDS_DATA": 1})
        self.assertEqual(history["decision_counts"], {"HOLD": 2})
        self.assertEqual([item["said"] for item in history["decided"]], ["观望一", "观望二"])
        self.assertEqual(history["decided"][-1]["revisit_when"], "跌破 0.40 再问")

    def test_the_screener_is_told_what_its_own_verdicts_led_to(self) -> None:
        self._screened("t1", "PRIORITIZE", 0.8)
        self._decided("t1", "HOLD", "观望", "跌破 0.40")
        self._screened("t2", "PRIORITIZE", 0.8)
        self._decided("t2", "BUY", "买入", "")
        calibration = self.memory.screening_calibration(platform="poly")
        self.assertEqual(calibration["by_screening_action"]["PRIORITIZE"], {"HOLD": 1, "BUY": 1})
        self.assertEqual(calibration["topics_with_history"], 2)

    def test_the_history_travels_with_the_candidate_and_the_state(self) -> None:
        source = Path("src/prediction_market_agent/runtime/market_discovery.py").read_text()
        self.assertIn('candidate["history"] = known', source)
        self.assertIn('"screening_calibration": self._screening_calibration', source)
        self.assertIn("self.memory.screening_history(platform=plugin.name)", source)

    def test_the_screener_is_asked_to_use_them(self) -> None:
        source = Path("src/prediction_market_agent/plugins/evaluators/jev.py").read_text()
        block = source[source.index('questions[f"route_{key}"]'):][:2200]
        for piece in ("history", "revisit_when", "already known", "screening_calibration"):
            self.assertIn(piece, block, piece)
