from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.agent.decision_evaluator import (
    CandidateAssessment, DecisionEvaluatorBudgetExhausted, DecisionEvaluatorError,
    DecisionEvaluatorPool,
)
from prediction_market_agent.plugin_system.config import PluginDirectoryConfig
from prediction_market_agent.plugin_system.discovery import (
    PluginInitializationContext, discover_plugin_catalog,
)
from prediction_market_agent.plugins.evaluators.laya import LayaDecisionEvaluator, initialize_plugin
from prediction_market_agent.plugins.evaluators.jev import SchemaDecisionEvaluator, _opener


class _LayaHandler(BaseHTTPRequestHandler):
    backend = "webgpu"
    requests: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        self._reply({
            "ready": True, "backend": self.backend, "model": "convaiinnovations/laya",
            "surface": {"takes": {"questions": {
                "types": {"choice": {}, "score": {}, "noul": {}}, "max": 6,
            }}},
        })

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.requests.append(body)
        asked = json.loads(body["messages"][-1]["content"])
        answers = {}
        for name, question in asked["questions"].items():
            kind = question["type"]
            if kind == "choice":
                choice = next(iter(question["criteria"]))
                answers[name] = {"type": kind, "choice": choice,
                                 "confidence": 0.95, "probabilities": {choice: 0.95}}
            elif kind == "score":
                answers[name] = {"type": kind, "score": 3, "confidence": 0.95}
            else:
                answers[name] = {"type": kind, "noul": 0.9, "confidence": 0.95}
        self._reply({"choices": [{"message": {"content": json.dumps({"answers": answers})}}]})

    def _reply(self, data: dict) -> None:
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        pass


class LayaPluginTests(unittest.TestCase):
    def test_budget_exhaustion_does_not_cool_down_or_fall_back_to_another_service(self) -> None:
        class Budgeted:
            name = "budgeted"

            def evaluate_candidates(self, _state, _candidates):
                raise DecisionEvaluatorBudgetExhausted("time budget")

        class Fallback:
            name = "fallback"

            def __init__(self):
                self.calls = 0

            def evaluate_candidates(self, _state, _candidates):
                self.calls += 1
                return [CandidateAssessment("one", "DEFER", 0.3)]

        fallback = Fallback()
        pool = DecisionEvaluatorPool([Budgeted(), fallback])
        self.assertEqual(pool.evaluate_candidates({}, [{"candidate_id": "one"}]), [])
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(pool._cooldown_until, {})

    def test_builtin_catalog_discovers_laya_as_an_independent_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directories = PluginDirectoryConfig.load(
                root / "directories.json", working_directory=root
            )
            catalog = discover_plugin_catalog(
                directories, enabled={"decision_evaluator": ("laya",)},
                working_directory=root, shared_http_proxy="DIRECT",
            )
            try:
                self.assertEqual(catalog.get("decision_evaluator", "laya").name, "laya")
            finally:
                catalog.shutdown()

    def setUp(self) -> None:
        _LayaHandler.backend = "webgpu"
        _LayaHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LayaHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _plugin(self, root: Path):
        config = root / "config" / "plugins"
        config.mkdir(parents=True)
        (config / "laya.json").write_text(json.dumps({
            "LAYA_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
            "LAYA_HTTP_PROXY": "DIRECT", "LAYA_TIMEOUT_SECONDS": 3,
        }))
        return initialize_plugin(PluginInitializationContext(
            kind="decision_evaluator", module_path=Path("laya.py"),
            working_directory=root, shared_http_proxy="DIRECT",
        ))

    def test_plugin_uses_typed_protocol_with_four_questions_per_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = self._plugin(Path(directory))
            self.assertTrue(plugin.readiness().ready)
            self.assertEqual(_LayaHandler.requests, [])  # Health checks never run inference.
            evaluator = plugin.factory(None)
            self.assertEqual(evaluator.name, "laya")
            self.assertEqual(evaluator.benchmark_status["status"], "ok")
            self.assertEqual(len(_LayaHandler.requests), 4)  # warm-up plus 3 timed samples
            _LayaHandler.requests.clear()
            results = evaluator.evaluate_candidates({}, [
                {"candidate_id": "one", "title": "First"},
                {"candidate_id": "two", "title": "Second"},
            ])
            self.assertEqual([item.candidate_id for item in results], ["one", "two"])
            self.assertEqual(len(_LayaHandler.requests), 2)
            for request in _LayaHandler.requests:
                self.assertEqual(request["model"], "convaiinnovations/laya")
                self.assertEqual(len(json.loads(request["messages"][-1]["content"])["questions"]), 4)
                self.assertEqual(request["response_format"]["type"], "json_schema")
            plugin.teardown()

    def test_scan_deadline_caps_network_timeout_without_entering_model_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evaluator = self._plugin(Path(directory)).factory(None)
            timeouts: list[float] = []
            real = _opener("")

            class Opener:
                def open(self, request, timeout):
                    timeouts.append(timeout)
                    return real.open(request, timeout=timeout)

            with patch("prediction_market_agent.plugins.evaluators.jev._opener",
                       return_value=Opener()):
                answer = evaluator.evaluate_candidates(
                    {"_scan_deadline_monotonic": time.monotonic() + 1},
                    [{"candidate_id": "one", "title": "Test"}],
                )
            self.assertEqual(len(answer), 1)
            self.assertLessEqual(timeouts[0], 1)
            asked = json.loads(_LayaHandler.requests[-1]["messages"][-1]["content"])
            self.assertNotIn("_scan_deadline_monotonic", asked["state"])

    def test_cpu_service_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = self._plugin(Path(directory))
            _LayaHandler.backend = "cpu"
            self.assertFalse(plugin.readiness().ready)
            with self.assertRaisesRegex(ValueError, "WebGPU"):
                plugin.factory(None)

    def test_laya_plugin_itself_serializes_concurrent_gpu_requests(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        active = peak = 0
        lock = threading.Lock()

        def fake_request(_self, state, _questions):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            return {"answers": {}, "model": str(state["number"])}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda number: evaluator._evaluate(
                    {"number": number}, {"q": {"type": "noul"}}
                ), range(8)))
        self.assertEqual(peak, 1)
        self.assertEqual({item["model"] for item in results}, {str(i) for i in range(8)})

    def test_queue_wait_expiry_does_not_send_a_second_gpu_request(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        evaluator.queue_wait_seconds = 0.02
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def fake_request(_self, _state, _questions):
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(1)
            return {"answers": {}}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(evaluator._evaluate, {}, {"q": {"type": "noul"}})
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(DecisionEvaluatorError, "queue wait timed out"):
                    evaluator._evaluate({}, {"q": {"type": "noul"}})
                release.set()
                first.result(timeout=1)
        self.assertEqual(calls, 1)

    def test_service_question_cap_and_option_quality_cap_are_distinct(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3,
                                          max_questions=6)
        with self.assertRaisesRegex(DecisionEvaluatorError, "at most 6 questions"):
            evaluator._evaluate({}, {str(i): {"type": "noul"} for i in range(7)})
        with self.assertRaisesRegex(DecisionEvaluatorError, "above 20 options"):
            evaluator._evaluate({}, {"q": {"type": "choice", "criteria": {
                str(i): str(i) for i in range(21)
            }}})

    def test_candidate_state_and_instructions_are_compact_before_request(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        captured = {}

        def fake_request(_self, state, questions):
            captured.update({"state": state, "questions": questions})
            return {"answers": {}}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            evaluator._evaluate(
                {"workflow": "candidate_evaluation", "run_state": {"large": "x" * 2000},
                 "candidates": [{"key": "c0", "question": "Market?", "description": "x" * 900,
                                 "status": "OPEN", "outcomes": [{"displayed_probability": 0.4}]}]},
                {"route_c0": {"type": "choice", "instructions": "y" * 900,
                               "criteria": {"DEFER": "later", "NEEDS_DATA": "missing"}}},
            )
        self.assertLess(len(json.dumps(captured["state"])), 500)
        self.assertLess(len(captured["questions"]["route_c0"]["instructions"]), 300)

    def test_benchmark_holds_queue_for_all_samples(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        first_sample = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_request(_self, state, _questions):
            label = "benchmark" if state.get("workflow") == "candidate_evaluation" else "normal"
            calls.append(label)
            if label == "benchmark" and len(calls) == 1:
                first_sample.set()
                release.wait(2)
            return {"answers": {}}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                measured = pool.submit(evaluator.run_benchmark)
                self.assertTrue(first_sample.wait(1))
                normal = pool.submit(evaluator._evaluate, {}, {"q": {"type": "noul"}})
                release.set()
                self.assertEqual(measured.result(timeout=3)["samples"], 3)
                normal.result(timeout=3)
        self.assertEqual(calls, ["benchmark"] * 4 + ["normal"])

    def test_periodic_benchmark_and_shutdown(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        self.assertEqual(evaluator.benchmark_interval_seconds, 300)
        evaluator.benchmark_interval_seconds = 0.06  # deterministic scheduler test
        calls = 0

        def fake_request(_self, _state, _questions):
            nonlocal calls
            calls += 1
            return {"answers": {}}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            evaluator.start_benchmarks()
            self.assertEqual(calls, 4)
            deadline = time.monotonic() + 1
            while calls < 8 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(calls, 8)
            evaluator.close()
            stopped_at = calls
            time.sleep(0.15)
            self.assertEqual(calls, stopped_at)

    def test_failed_benchmark_releases_queue_for_normal_calls(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)
        calls = 0

        def fake_request(_self, _state, _questions):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DecisionEvaluatorBudgetExhausted("synthetic GPU timeout")
            return {"answers": {}}

        with patch.object(SchemaDecisionEvaluator, "_evaluate", fake_request):
            self.assertEqual(evaluator.run_benchmark()["status"], "failed")
            self.assertEqual(
                evaluator._evaluate({}, {"q": {"type": "noul"}}), {"answers": {}}
            )
        self.assertEqual(calls, 2)

    def test_scan_budget_timeout_is_not_reported_as_gpu_failure(self) -> None:
        evaluator = LayaDecisionEvaluator("http://127.0.0.1:8899/v1", "", 3)

        def timed_out(_self, _state, _questions):
            time.sleep(0.025)
            raise DecisionEvaluatorError("request timed out")

        with patch.object(SchemaDecisionEvaluator, "_evaluate", timed_out):
            with self.assertRaisesRegex(DecisionEvaluatorBudgetExhausted, "scan ended"):
                evaluator._evaluate(
                    {"_scan_deadline_monotonic": time.monotonic() + 0.015},
                    {"q": {"type": "noul"}},
                )
            with self.assertRaisesRegex(DecisionEvaluatorError, "Laya GPU request timed out"):
                evaluator._evaluate({}, {"q": {"type": "noul"}})


class EvaluatorOrderingTests(unittest.TestCase):
    class Screener:
        def __init__(self, name: str, calls: list[str], *, fails: bool = False) -> None:
            self.name, self.calls, self.fails = name, calls, fails

        def evaluate_candidates(self, _state, _candidates):
            self.calls.append(self.name)
            if self.fails:
                raise RuntimeError("unavailable")
            return [CandidateAssessment("one", "PRIORITIZE", 0.75, provider=self.name)]

    def test_quality_first_and_configured_order_both_fall_back(self) -> None:
        calls: list[str] = []
        pool = DecisionEvaluatorPool([
            self.Screener("first", calls), self.Screener("second", calls, fails=True),
            self.Screener("third", calls),
        ])
        pool.set_quality({"first": 0.8, "second": 1.3, "third": 1.1})
        answer = pool.evaluate_candidates({}, [{"candidate_id": "one"}])
        self.assertEqual(calls, ["second", "third"])
        self.assertEqual(answer[0].evaluator_name, "third")
        self.assertEqual(pool.errors, {})

        calls.clear()
        pool.set_order_mode("CONFIGURED")
        answer = pool.evaluate_candidates({}, [{"candidate_id": "one"}])
        self.assertEqual(calls, ["first"])
        self.assertEqual(answer[0].evaluator_name, "first")

    def test_all_failures_are_reported_without_a_fake_answer(self) -> None:
        calls: list[str] = []
        pool = DecisionEvaluatorPool([
            self.Screener("first", calls, fails=True),
            self.Screener("second", calls, fails=True),
        ])
        self.assertEqual(pool.evaluate_candidates({}, [{"candidate_id": "one"}]), [])
        self.assertEqual(set(pool.errors), {"first", "second"})


if __name__ == "__main__":
    unittest.main()
