from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from prediction_market_agent.core.config import Config
from prediction_market_agent.runtime.auth import AdminAuthStore
from prediction_market_agent.runtime.dashboard import create_app


class LocalAccessTests(unittest.TestCase):
    def test_loopback_hosts_use_plaintext_without_a_passkey_or_crypto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json")
            app = create_app(config, start_robot=False)
            for host in ("127.0.0.1", "127.0.0.2", "localhost", "[::1]"):
                with self.subTest(host=host), TestClient(app, base_url="http://localhost", headers={"Host": host}) as client, patch.object(
                    AdminAuthStore, "decrypt", side_effect=AssertionError("Local request used crypto")
                ), patch.object(AdminAuthStore, "session", side_effect=AssertionError("Local request used a session")):
                    self.assertIn("LOCAL_ACCESS=true", client.get("/").text)
                    self.assertFalse(client.get("/api/auth/status").json()["authentication_required"])
                    response = client.post("/api/local", json={"url": "/api/settings", "body": None})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("fields", response.json())
                    saved = client.post("/api/local", json={"url": "/api/settings",
                        "body": {"values": {"dashboard_refresh_seconds": 9}}})
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
            for host in ("robot.example", "localhost.evil.example", "192.168.1.2"):
                with self.subTest(host=host), TestClient(app, base_url=f"https://{host}") as client:
                    self.assertIn("初始化管理员", client.get("/").text)
                    result = client.post("/api/local", json={"url": "/api/settings"},
                        headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost"})
                    self.assertEqual(result.status_code, 403)
                    self.assertEqual(client.post("/api/secure", json={}).status_code, 401)
            with TestClient(app, base_url="http://localhost") as client:
                for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
                    self.assertEqual(client.post("/api/local", json={"url": "/api/settings"}, headers=headers).status_code, 403)
                self.assertEqual(client.post("/api/local", content='{}', headers={"Content-Type": "text/plain"}).status_code, 415)
