from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from prediction_market_agent.runtime import cloud


ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_local_and_container_shell_entries_are_valid(self) -> None:
        for relative in (
            "start-local.sh",
            "deploy/container-entrypoint.sh",
            "deploy/container-with-worker.sh",
            "deploy/aliyun/build.sh",
            "deploy/ensure-docker.sh",
            "deploy/check-container.sh",
        ):
            completed = subprocess.run(
                ["sh", "-n", str(ROOT / relative)], capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_platform_configuration_files_have_required_entries(self) -> None:
        vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        railway = tomllib.loads((ROOT / "railway.toml").read_text(encoding="utf-8"))
        aws = (ROOT / "deploy/aws/template.yaml").read_text(encoding="utf-8")
        aliyun = (ROOT / "deploy/aliyun/s.yaml").read_text(encoding="utf-8")
        self.assertIn("api/index.py", vercel["functions"])
        self.assertEqual(railway["deploy"]["healthcheckPath"], "/healthz")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FORWARDED_ALLOW_IPS=127.0.0.1,100.0.0.0/8", dockerfile)
        self.assertIn("prediction_market_agent.runtime.cloud.lambda_handler", aws)
        self.assertNotIn("Type: ScheduleV2", aws)
        self.assertIn("custom.debian12", aliyun)
        self.assertNotIn("triggerType: timer", aliyun)
        self.assertIn("nasConfig: auto", aliyun)

    def test_function_compute_reserved_invoke_route_runs_one_cycle(self) -> None:
        with patch.object(cloud, "run_once_event", return_value={"decisions": 0}) as run:
            with TestClient(cloud.application, base_url="http://localhost") as client:
                denied = client.post("/invoke")
                accepted = client.post(
                    "/invoke", headers={"x-fc-control-path": "/invoke"}, content=b"{}"
                )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["result"], {"decisions": 0})
        run.assert_called_once_with()

    def test_cloud_bootstrap_selects_database_paths_before_app_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = root / "application.json"
            selection = root / "selection.json"
            selection.write_text(
                '{"enabled":{"api":["binance"],"decision_provider":["codex"]},'
                '"decision_strategy":"general_agent"}',
                encoding="utf-8",
            )
            application.write_text(
                json.dumps({"version": 1, "values": {
                    "working_directory": str(root),
                    "management_file": str(selection),
                    "session_db": "chosen/decision.sqlite3",
                    "auth_db": "chosen/auth.sqlite3",
                }}),
                encoding="utf-8",
            )
            from prediction_market_agent.core.config import Config

            config = Config.load(application)
            self.assertEqual(config.session_db, (root / "chosen/decision.sqlite3").resolve())
            self.assertEqual(config.auth_db, (root / "chosen/auth.sqlite3").resolve())
            self.assertFalse(config.session_db.exists())
            self.assertFalse(config.auth_db.exists())


if __name__ == "__main__":
    unittest.main()
