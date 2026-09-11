from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LocalLauncherTests(unittest.TestCase):
    def test_detected_but_unusable_proxy_requires_explicit_direct_or_exits(self):
        for action, succeeds in (("direct", True), ("", False)):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "deploy").mkdir()
                shutil.copyfile(ROOT / "deploy/prepare-host-proxy.sh", root / "deploy/prepare-host-proxy.sh")
                shutil.copyfile(ROOT / "deploy/ensure-docker.sh", root / "deploy/ensure-docker.sh")
                (root / "deploy/host-proxy.sh").write_text("printf 'complete\\tdGVzCg==\\n'\n")
                (root / "deploy/host-proxy.py").write_text("# mounted fixture\n")
                bin_dir = root / "bin"
                bin_dir.mkdir()
                docker = bin_dir / "docker"
                docker.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\\n" "$*" >> "$LAUNCH_LOG"\n'
                    'case "$1" in info) exit 0;; esac\n'
                    'case "$*" in\n'
                    '  *" --verify") echo "Proxy validation failed: yes";;\n'
                    '  *" --force-direct") echo "Host proxy: user selected direct / direct";;\n'
                    '  *) echo "Host proxy: fixture / proxy";;\n'
                    'esac\n',
                    encoding="utf-8",
                )
                docker.chmod(0o755)
                log = root / "calls"
                environment = {
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "LAUNCH_LOG": str(log),
                    "PREDICTION_AGENT_PROXY_FAILURE_ACTION": action,
                }
                result = subprocess.run(
                    ["sh", str(root / "deploy/prepare-host-proxy.sh"), "fixture:image"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                calls = log.read_text(encoding="utf-8")
                self.assertIn("--verify", calls)
                self.assertEqual("--force-direct" in calls, succeeds)
                if not succeeds:
                    self.assertIn("未启动机器人", result.stderr)

    def test_existing_docker_pulls_then_starts_and_stops_on_pull_error(self):
        for pull_fails in (False, True):
            with self.subTest(pull_fails=pull_fails), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "deploy").mkdir()
                for name in ("start-local.sh", "compose.yaml", "deploy/ensure-docker.sh"):
                    shutil.copyfile(ROOT / name, root / name)
                # The launcher's contract is that it obtains the image through the pull driver and
                # stops when that fails. The driver decides for itself how to reach the registry
                # and is covered separately; standing in for it here keeps this test off the network.
                (root / "deploy/registry-pull.py").write_text(
                    "import subprocess, sys\n"
                    "raise SystemExit(subprocess.run(['docker', 'pull', sys.argv[1]]).returncode)\n"
                )
                (root / "deploy/local-callbacks.sh").write_text('echo ready > "$3"\n')
                (root / "deploy/prepare-host-proxy.sh").write_text('printf "proxy-prepare %s\\n" "$*" >> "$LAUNCH_LOG"\n')
                bin_dir = root / "bin"
                bin_dir.mkdir()
                uname = bin_dir / "uname"
                uname.write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
                uname.chmod(0o755)
                docker = bin_dir / "docker"
                docker.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCH_LOG"\n'
                    'case "$*" in pull*|*" pull") [ "$PULL_FAILS" != true ] ;; *" ps -q robot") echo abc123 ;; *) exit 0 ;; esac\n',
                    encoding="utf-8",
                )
                docker.chmod(0o755)
                log = root / "calls"
                result = subprocess.run(["sh", str(root / "start-local.sh")],
                    env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                         "LAUNCH_LOG": str(log), "PULL_FAILS": str(pull_fails).lower()},
                    capture_output=True, text=True, timeout=10)
                calls = log.read_text(encoding="utf-8")
                self.assertIn("pull", calls, "the launcher must obtain the image before starting")
                self.assertEqual(result.returncode == 0, not pull_fails, result.stderr)
                self.assertEqual("up -d --no-build --wait" in calls, not pull_fails)
                if not pull_fails:
                    self.assertLess(calls.index("proxy-prepare"), calls.index("up -d"))
                self.assertFalse((root / ".venv").exists())

    def test_linux_stopped_engine_is_started_before_using_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "uname": 'echo Linux',
                "id": 'echo 0',
                "dockerd": 'exit 0',
                "docker": 'if [ "$1" = info ]; then [ -f "$ENGINE_READY" ]; else exit 0; fi',
                "systemctl": 'test "$1 $2" = "start docker" && touch "$ENGINE_READY"',
            }.items():
                command = root / name
                command.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
                command.chmod(0o755)
            script = ROOT / "deploy/ensure-docker.sh"
            result = subprocess.run(["sh", "-c", '. "$1"; ensure_docker', "sh", str(script)],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "ENGINE_READY": str(root / "ready")}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "ready").exists())
