from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prediction_market_agent.agent.decision_evaluator import (
    CandidateAssessment, DecisionEvaluatorPool,
)
from prediction_market_agent.plugin_system.config import PluginDirectoryConfig
from prediction_market_agent.plugin_system.discovery import (
    PluginInitializationContext, discover_plugin_catalog,
)
from prediction_market_agent.plugins.evaluators.laya import initialize_plugin


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
            evaluator = plugin.factory(None)
            self.assertEqual(evaluator.name, "laya")
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

    def test_cpu_service_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = self._plugin(Path(directory))
            _LayaHandler.backend = "cpu"
            self.assertFalse(plugin.readiness().ready)
            with self.assertRaisesRegex(ValueError, "WebGPU"):
                plugin.factory(None)


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
