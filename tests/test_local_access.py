from __future__ import annotations

import tempfile
import unittest
import io
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugin_system.managed_config import ManagedRuntimeConfig
from prediction_market_agent.runtime.auth import AdminAuthStore
from prediction_market_agent.runtime.dashboard import create_app
from prediction_market_agent.runtime.memory import SessionMemory


class LocalAccessTests(unittest.TestCase):
    def test_screening_records_route_excludes_unassessed_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json")
            memory = SessionMemory(config.session_db)
            memory.record_topic_observations(platform="venue", observations=[
                {"market_topic_id": "one", "title": "One", "status": "OPEN",
                 "liquidity_usdt": 10, "volume_usdt": 20,
                 "features": {"typed_evaluation": {"action": "DEFER", "confidence": 0.9}}},
                {"market_topic_id": "two", "title": "Two", "status": "OPEN",
                 "liquidity_usdt": 10, "volume_usdt": 20, "features": {}},
            ])
            memory.close()
            with TestClient(create_app(config, start_robot=False), base_url="http://localhost") as client:
                response = client.post("/api/local", json={"url":
                    "/api/discovery/screenings?platform=venue&limit=1&offset=0", "body": None})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total"], 1)
                self.assertEqual(response.json()["items"][0]["market_topic_id"], "one")

    def test_plugin_selection_is_saved_before_runtime_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
            )
            with patch("prediction_market_agent.runtime.controller.RobotRuntimeManager.reconcile"), patch(
                "prediction_market_agent.runtime.controller.RobotRuntimeManager.stop"
            ) as stop, patch(
                "prediction_market_agent.runtime.controller.RobotRuntimeManager.reconcile_async"
            ) as reconcile_async, TestClient(
                create_app(config, start_robot=True), base_url="http://localhost"
            ) as client:
                manifest = client.post("/api/local", json={"url": "/api/plugins/manage", "body": None}).json()
                enabled = {
                    kind: [item["name"] for item in items if item["enabled"]]
                    for kind, items in manifest["plugins"].items()
                }
                enabled["decision_evaluator"] = []
                response = client.post("/api/local", json={"url": "/api/plugins/selection", "body": {
                    "enabled": enabled, "decision_strategy": "", "strategy_evolution": True,
                }})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(ManagedRuntimeConfig.load(config.management_file).enabled["decision_evaluator"], ())
                self.assertFalse(any(item["enabled"] for item in response.json()["plugins"]["decision_evaluator"]))
                stop.assert_not_called()
                reconcile_async.assert_called_once_with()

    def test_model_selection_mode_is_saved_without_replacing_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
            )
            with TestClient(create_app(config, start_robot=False), base_url="http://localhost") as client:
                for values in ({"paper_trading": "on"}, {"model_selection_mode": "CONFIGURED"}):
                    response = client.post("/api/local", json={"url": "/api/settings", "body": {"values": values}})
                    self.assertEqual(response.status_code, 200)
                fields = client.post("/api/local", json={"url": "/api/settings", "body": None}).json()["fields"]
                effective = {field["name"]: field["value"] for field in fields}
                self.assertEqual(effective["model_selection_mode"], "CONFIGURED")
                self.assertEqual(effective["paper_trading"], "on")

    def test_laya_probe_distinguishes_loading_from_disconnected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json")
            endpoint = "http://host.docker.internal:8899/v1"
            health = "http://host.docker.internal:8899/health"
            loading = urllib.error.HTTPError(health, 503, "Service Unavailable", None,
                io.BytesIO(b'{"ready":false,"status":"loading model"}'))
            with TestClient(create_app(config, start_robot=False), base_url="http://localhost") as client:
                with patch("prediction_market_agent.runtime.dashboard.urllib.request.urlopen",
                           side_effect=loading) as urlopen:
                    response = client.post("/api/local", json={"url": "/api/laya/probe",
                        "body": {"endpoint": endpoint}})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"reachable": True, "ready": False,
                    "backend": "", "model": "", "status": "loading model"})
                urlopen.assert_called_once_with(health, timeout=8)

    def test_responsive_assets_are_public_but_only_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=Config(working_directory=root,session_db=root/'sessions.sqlite3',auth_db=root/'auth.sqlite3',
                          management_file=root/'selection.json',plugin_directories_file=root/'directories.json')
            with TestClient(create_app(config,start_robot=False),base_url='https://robot.example') as client:
                for asset,kind in [('dashboard.css','text/css'),('dashboard-shell.js','application/javascript')]:
                    response=client.get('/assets/'+asset)
                    self.assertEqual(response.status_code,200)
                    self.assertIn(kind,response.headers['content-type'])
                self.assertEqual(client.get('/assets/not-public.json').status_code,404)
                self.assertIn('/assets/dashboard.css',client.get('/').text)
                self.assertEqual(client.post('/api/local',json={'url':'/api/plugins/controls'}).status_code,403)

    def test_loopback_hosts_use_plaintext_without_a_passkey_or_crypto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json")
            app = create_app(config, start_robot=False)
            for host in ("127.0.0.1", "127.0.0.2", "localhost", "[::1]", "192.168.1.2",
                         "10.2.3.4", "172.16.0.1", "172.31.255.254", "169.254.1.2",
                         "[fd12::1]", "[fe80::1]", "[::ffff:192.168.1.2]"):
                with self.subTest(host=host), TestClient(app, base_url="http://localhost", headers={"Host": host}) as client, patch.object(
                    AdminAuthStore, "decrypt", side_effect=AssertionError("Local request used crypto")
                ), patch.object(AdminAuthStore, "session", side_effect=AssertionError("Local request used a session")):
                    self.assertIn("LOCAL_ACCESS=true", client.get("/").text)
                    self.assertFalse(client.get("/api/auth/status").json()["authentication_required"])
                    response = client.post("/api/local", json={"url": "/api/settings", "body": None})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("fields", response.json())
                    saved = client.post("/api/local", json={"url": "/api/settings",
                        "body": {"values": {"environment_probe_timeout": 9}}})
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual(len(client.cookies), 0)
            self.assertFalse(AdminAuthStore(config.auth_db, 72, 168).has_admin())

    def test_public_and_cross_origin_requests_cannot_use_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json")
            app = create_app(config, start_robot=False)
            for host in ("robot.example", "localhost.evil.example", "192.169.1.2",
                         "172.15.1.1", "172.32.0.1", "0.0.0.0", "[::]", "100.64.0.1",
                         "192.0.2.1", "224.0.0.1", "[2001:db8::1]"):
                with self.subTest(host=host), TestClient(app, base_url="https://robot.example", headers={"Host": host}) as client:
                    self.assertIn("初始化管理员", client.get("/").text)
                    result = client.post("/api/local", json={"url": "/api/settings"},
                        headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost"})
                    self.assertEqual(result.status_code, 403)
                    self.assertEqual(client.post("/api/secure", json={}).status_code, 401)
            with TestClient(app, base_url="http://localhost") as client:
                for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
                    self.assertEqual(client.post("/api/local", json={"url": "/api/settings"}, headers=headers).status_code, 403)
                self.assertEqual(client.post("/api/local", content='{}', headers={"Content-Type": "text/plain"}).status_code, 415)
