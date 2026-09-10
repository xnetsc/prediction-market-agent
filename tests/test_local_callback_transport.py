from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_callbacks", ROOT / "deploy/local-callbacks.py")
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class LocalCallbackTransportTests(unittest.TestCase):
    def status(self):
        return {"items": [{"status": {"state": "authorizing", "flow_id": "current-flow",
            "expires_at": 120, "redirect_uri": "http://localhost:34567/callback",
            "callback_probe": {"url": "http://localhost:34567/.well-known/prediction-login/probe",
                               "expected_proof": "per-flow-proof"}}}]}

    def test_only_active_proven_loopback_listeners_are_candidates(self):
        self.assertEqual(transport.active_ports(self.status(), 100), {34567: ("current-flow", 20)})
        for field, value in (("state", "authenticated"), ("callback_mode", "helper"), ("callback_pending", False),
                             ("expires_at", 99), ("flow_id", ""), ("callback_probe", None),
                             ("redirect_uri", "http://public.example:34567/callback"),
                             ("redirect_uri", "http://localhost:34567/callback?code=secret")):
            with self.subTest(field=field, value=value):
                status = self.status()
                status["items"][0]["status"][field] = value
                self.assertFalse(transport.active_ports(status, 100))

    def test_end_and_same_port_new_flow_remove_old_publication(self):
        previous = {34567: ("old", 60)}
        self.assertEqual(list(transport.changes(previous, {})), ["REMOVE 34567 0"])
        self.assertEqual(list(transport.changes(previous, {34567: ("new", 30)})),
                         ["REMOVE 34567 0", "ADD 34567 30"])
        self.assertFalse(list(transport.changes(previous, {34567: ("old", 59)})))

    def test_shell_publisher_is_scoped_and_reports_port_conflicts(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("local-callbacks.sh", "local-callbacks.py", "ensure-docker.sh"):
                    shutil.copyfile(ROOT / "deploy" / name, root / name)
                docker = root / "docker"
                docker.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CALLBACK_TEST_LOG'],'a') as log: log.write(json.dumps(args)+'\\n')
robot='a'*64
if args[0]=='inspect':
 fmt=args[2]
 print(robot if '.Id' in fmt or '.Config.Labels' in fmt else
       'sha256:fixture-image' if '.Image' in fmt else
       '172.18.0.2' if 'IPAddress' in fmt else 'test-network')
elif args[0]=='exec':
 sys.stdin.read()
 print('READY 0 0\\nADD 34567 60\\nREMOVE 34567 0\\nADD 1455 60')
elif args[0]=='port':
 if args[-1]=='1455/tcp': print('127.0.0.1:1455')
 else: sys.exit(1)
elif args[0]=='run' and os.environ['CALLBACK_TEST_CONFLICT']=='1': sys.exit(125)
''')
                docker.chmod(0o755)
                log, ready = root / "commands.jsonl", root / "ready"
                result = subprocess.run(["sh", str(root / "local-callbacks.sh"), "robot", "8765", str(ready)],
                    env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                         "CALLBACK_TEST_LOG": str(log), "CALLBACK_TEST_CONFLICT": str(int(conflict))},
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(ready.read_text().strip(), "ready")
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                runs = [call for call in calls if call[0] == "run"]
                self.assertEqual(len(runs), 1)
                self.assertIn("127.0.0.1:34567:34567", runs[0])
                self.assertIn("test-network", runs[0])
                self.assertIn("172.18.0.2", runs[0])
                self.assertIn("--read-only", runs[0])
                self.assertNotIn("docker.sock", str(runs))
                self.assertNotIn("--privileged", str(runs))
                self.assertEqual("could not be published" in result.stderr, conflict)

    def test_tunnel_rejects_non_container_targets(self):
        for target in ("127.0.0.1", "0.0.0.0", "8.8.8.8"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                transport.tunnel(target, 34567, 1)
