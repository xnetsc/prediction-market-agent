from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.providers import codex, claude
from prediction_market_agent.plugins.providers._client_control import ClientControl
from prediction_market_agent.plugins.providers._login_relay import BrowserLoginRelay, seal


CLIENT = '''#!EXECUTABLE
import json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlsplit
from pathlib import Path
name=Path(sys.argv[0]).name
home=Path(os.environ['HOME'])
account=home/'fixture-account.json'
args=sys.argv[1:]
if '--version' in args:
 print('2.1.0');sys.exit(0)
if 'status' in args:
 if name=='claude': print(json.dumps({'loggedIn':account.exists()}))
 else: print('Logged in using ChatGPT' if account.exists() else 'Not logged in')
 sys.exit(0 if account.exists() else 1)
if 'logout' in args:
 account.unlink(missing_ok=True);sys.exit(0)
if 'login' in args:
 if '--device-auth' in args:
  import time
  print('https://auth.openai.com/codex/device\\nEnter code: ABCD-EFGH',flush=True)
  while not (home/'fixture-approval').exists(): time.sleep(.01)
  account.write_text('fixture-persisted-token')
  sys.exit(0)
 if name=='claude' and 'exit 1' in Path(os.environ['BROWSER']).read_text():
  print('https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback\\nPaste code here if prompted > ',flush=True)
  if sys.stdin.readline().strip() != 'fixture-approval#fixture-state': sys.exit(1)
  account.write_text('fixture-persisted-token')
  sys.exit(0)
 class Handler(BaseHTTPRequestHandler):
  def do_GET(self):
   if name=='codex' and urlsplit(self.path).path=='/success' and account.exists():
    (home/'fixture-completion').touch()
    self.server.finished=True
    self.send_response(200);self.end_headers();return
   query=parse_qs(urlsplit(self.path).query)
   ok=query.get('state')==['fixture'] and query.get('code')==['fixture-approval']
   if ok: account.write_text('fixture-persisted-token')
   if ok and name=='codex':
    self.send_response(302);self.send_header('Location','/success');self.end_headers();return
   self.server.finished=ok
   self.send_response(200 if ok else 403);self.end_headers()
  def log_message(self,*args): pass
 server=HTTPServer(('127.0.0.1',0),Handler)
 path='/auth/callback' if name=='codex' else '/callback'
 redirect='http://localhost:'+str(server.server_port)+path
 url=('https://auth.openai.com/oauth/authorize?' if name=='codex' else 'https://claude.com/cai/oauth/authorize?')+urlencode({'state':'fixture','redirect_uri':redirect})
 print(url,flush=True)
 server.finished=False
 while not server.finished: server.handle_request()
 server.server_close()
 print('private-output-do-not-display',flush=True)
 sys.exit(0)
sys.exit(3)
'''


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Condition was not reached")


class ClientAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.start_patch = patch.object(ClientControl, "start")
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)

    def plugin(self, name="claude"):
        command = self.root / name
        command.write_text(CLIENT.replace("EXECUTABLE", sys.executable))
        command.chmod(0o700)
        module = codex if name == "codex" else claude
        spec = module.initialize_plugin(PluginInitializationContext("decision_provider", command, self.root))
        spec.configuration.save({f"{name.upper()}_CLI_PATH": str(command),
                                 f"{name.upper()}_CHECK_UPDATES": False})
        self.addCleanup(spec.teardown)
        return spec, spec.controls.status_callback.__self__

    def test_remote_claude_code_is_delivered_without_a_callback_listener(self):
        spec, control = self.plugin('claude')
        control.action('login_remote', {})
        wait_for(lambda: bool(control.snapshot().get('authorization_url')))
        state = control.snapshot()
        self.assertIsNone(control.relay)
        self.assertEqual(state['login_mode'], 'remote')
        with self.assertRaises(ValueError):
            control.action('login_code', {'flow_id':'stale','code':'fixture-approval#fixture-state'})
        with self.assertRaises(ValueError):
            control.action('login_code', {'flow_id':control.flow_id,'code':'bad\ncode'})
        control.action('login_code', {'flow_id':control.flow_id,'code':'fixture-approval#fixture-state'})
        wait_for(lambda: control.snapshot()['state'] == 'authenticated')
        self.assertNotIn('fixture-approval', json.dumps(control.snapshot()))
        self.assertFalse(control.snapshot().get('authorization_url'))

    def test_remote_device_code_waits_for_official_confirmation_and_clears_on_cancel(self):
        spec, control = self.plugin('codex')
        control.action('login_remote', {})
        wait_for(lambda: bool(control.snapshot().get('device_code')))
        self.assertEqual(control.snapshot()['device_code'], 'ABCD-EFGH')
        self.assertIsNone(control.relay)
        self.assertEqual(control.snapshot()['state'], 'authorizing')
        control.cancel()
        self.assertFalse(control.snapshot()['device_code'])
        control.action('login_remote', {})
        wait_for(lambda: bool(control.snapshot().get('device_code')))
        (control.directory('AUTH')/'fixture-approval').touch()
        wait_for(lambda: control.snapshot()['state'] == 'authenticated')
        self.assertFalse(control.snapshot()['device_code'])

    def test_remote_invalid_code_is_not_a_success(self):
        spec, control = self.plugin('claude')
        control.action('login_remote', {})
        wait_for(lambda: bool(control.snapshot().get('authorization_url')))
        control.action('login_code', {'flow_id':control.flow_id,'code':'invalid-code'})
        wait_for(lambda: control.snapshot()['state'] == 'login_required')
        self.assertFalse((control.directory('AUTH')/'fixture-account.json').exists())

    def test_remote_expiry_clears_instructions_and_rejects_late_code(self):
        spec, control = self.plugin('claude')
        timer = threading.Timer
        with patch('prediction_market_agent.plugins.providers._client_control.threading.Timer',
                   side_effect=lambda seconds, callback: timer(.3,callback)):
            control.action('login_remote', {})
        flow = control.flow_id
        wait_for(lambda: control.snapshot()['state'] == 'login_required')
        self.assertIn('超时',control.snapshot()['message'])
        self.assertFalse(control.snapshot().get('authorization_url'))
        with self.assertRaises(ValueError):
            control.action('login_code', {'flow_id':flow,'code':'fixture-approval#fixture-state'})

    def test_official_authorization_instructions_persistence_and_logout(self):
        for name in ("codex", "claude"):
            with self.subTest(name=name):
                spec, control = self.plugin(name)
                control.inspect()
                self.assertEqual(control.snapshot()["state"], "login_required")
                control.action("login", {})
                wait_for(lambda: bool(control.relay.authorization_url))
                self.assertFalse(control.snapshot()["authorization_url"])
                with self.assertRaisesRegex(ValueError, "已启动"):
                    control.login()
                self.approve(control)
                wait_for(lambda: control.snapshot()["state"] == "authenticated")
                if name == "codex":
                    self.assertTrue((control.directory("AUTH") / "fixture-completion").exists())
                snapshot = json.dumps(control.snapshot())
                self.assertNotIn("private-output", snapshot)
                self.assertNotIn("fixture-persisted-token", snapshot)
                self.assertNotIn("fixture-approval", snapshot)
                self.assertEqual(control.snapshot()["authorization_url"], "")
                control.teardown()
                second = ClientControl(name, control.package, spec.configuration, self.root)
                self.addCleanup(second.teardown)
                second.inspect()
                self.assertEqual(second.snapshot()["state"], "authenticated")
                second.action("logout", {})
                self.assertEqual(second.snapshot()["state"], "login_required")
                self.assertFalse((second.directory("AUTH") / "fixture-account.json").exists())

    def test_proxy_and_credentials_are_scoped_to_the_provider(self):
        spec, control = self.plugin("codex")
        spec.configuration.save({**spec.configuration.load(), "CODEX_HTTP_PROXY": "http://proxy.example:8080"})
        with patch.dict(os.environ, {"HOME": "/host-home", "CODEX_HOME": "/host-account",
                                    "CLAUDE_CONFIG_DIR": "/another-account", "BINANCE_API_KEY": "not-for-cli"}):
            env = control.environment()
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:8080")
        self.assertEqual(env["HOME"], str(self.root / "credentials" / "codex"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("BINANCE_API_KEY", env)
        self.assertEqual((self.root / "credentials" / "codex").stat().st_mode & 0o777, 0o700)

    def test_retired_helper_directory_does_not_break_private_configuration(self):
        for name in ("codex", "claude"):
            spec, control = self.plugin(name)
            values = spec.configuration.load()
            values[name.upper() + "_HELPER_DIRECTORY"] = "/old/helpers"
            spec.configuration.save_callback(values)
            self.assertNotIn(name.upper() + "_HELPER_DIRECTORY", spec.configuration.load())
            self.assertEqual(control.executable(), values[name.upper() + "_CLI_PATH"])

    def test_host_proxy_reaches_both_cli_environments_without_exposing_credentials(self):
        snapshot = self.root / ".deployment" / "host-proxy.json"
        snapshot.parent.mkdir()
        snapshot.write_text(json.dumps({"status": "proxy", "proxy": "http://relay:fixture-private@proxy.example:8123",
                                        "source": "host-system", "no_proxy": "internal.example"}))
        for name in ("codex", "claude"):
            spec, control = self.plugin(name)
            self.assertEqual(spec.configuration.load()[name.upper() + "_HTTP_PROXY"], "INHERIT")
            env = control.environment()
            self.assertEqual(env["HTTPS_PROXY"], "http://relay:fixture-private@proxy.example:8123")
            self.assertIn("127.0.0.1", env["NO_PROXY"])
            self.assertIn("internal.example", env["NO_PROXY"])
            self.assertNotIn("fixture-private", control.snapshot()["proxy_message"])
            values = spec.configuration.load()
            values[name.upper() + "_HTTP_PROXY"] = "DIRECT"
            spec.configuration.save(values)
            self.assertNotIn("HTTPS_PROXY", control.environment())

    def test_script_command_is_bound_to_current_flow(self):
        _, control = self.plugin()
        control.action("login", {"_public_origin": "https://robot.example"})
        wait_for(lambda: bool(control.relay.authorization_url))
        with self.assertRaises(ValueError):
            control.action("helper_command", {"flow_id": "old", "platform": "bash", "_public_origin": "https://robot.example"})
        result = control.action("helper_command", {"flow_id": control.flow_id, "platform": "bash", "_public_origin": "https://robot.example"})
        ticket = next(iter(control.relay.script_tickets))
        self.assertIn(ticket, result["command"])
        self.assertIn("prediction_login_main", control.helper_message(ticket, {"script_platform": "bash"})["script"])
        with self.assertRaises(ValueError):
            control.helper_message(ticket, {"script_platform": "bash"})
        control.cancel()

    def test_auth_failures_persist_but_network_and_quota_failures_do_not(self):
        spec, control = self.plugin()
        home = Path(control.environment()["HOME"])
        (home / "fixture-account.json").write_text("fixture-persisted-token")
        control.record_auth_failure("HTTP 429 rate limit; network timeout")
        control.inspect()
        self.assertEqual(control.snapshot()["state"], "authenticated")
        control.record_auth_failure("HTTP 401 invalid_token private-fixture-secret")
        control.inspect()
        self.assertEqual(control.snapshot()["state"], "login_required")
        self.assertNotIn("private-fixture-secret", (home / "reauthentication.json").read_text())
        control.action("login", {})
        wait_for(lambda: control.relay.authorization_url)
        self.approve(control)
        wait_for(lambda: control.snapshot()["state"] == "authenticated")
        self.assertFalse((home / "reauthentication.json").exists())

    def test_cancel_timeout_and_unload_reap_login_process(self):
        _, control = self.plugin()
        control.login()
        process = control.process
        control.cancel()
        self.assertIsNotNone(process.poll())
        self.assertFalse(control.login_thread.is_alive())
        timer = threading.Timer
        with patch("prediction_market_agent.plugins.providers._client_control.threading.Timer",
                   side_effect=lambda interval, callback: timer(.2, callback)):
            control.login()
            wait_for(lambda: "超时" in control.snapshot()["message"])
        control.login()
        process = control.process
        control.teardown()
        self.assertIsNotNone(process.poll())
        with self.assertRaisesRegex(ValueError, "unloaded"):
            control.action("login", {})

    def test_only_official_https_authorization_links_are_displayed(self):
        _, control = self.plugin()
        control.relay = BrowserLoginRelay(60)
        control._login_instructions("https://evil.example/authorize https://claude.com.evil.example/x https://u:p@claude.com/x")
        self.assertFalse(control.snapshot().get("authorization_url"))
        control._login_instructions("https://claude.com/cai/oauth/authorize?state=test&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fcallback ")
        self.assertTrue(control.relay.authorization_url.startswith("https://claude.com/"))

    def approve(self, control):
        relay = control.relay
        relay.handle(relay.secret, seal(relay.secret,
            {"action": "ready", "redirect_uri": relay.redirect_uri}, "helper-request"))
        self.assertTrue(control.snapshot()["authorization_url"])
        relay.handle(relay.secret, seal(relay.secret,
            {"action": "callback", "query": "state=fixture&code=fixture-approval"}, "helper-request"))

    def test_upgrades_are_versioned_and_failed_upgrade_preserves_active_client(self):
        _, control = self.plugin()
        npm = self.root / "npm"
        npm.write_text('''#!EXECUTABLE
import sys
from pathlib import Path
target=Path(sys.argv[sys.argv.index('--prefix')+1])
version=sys.argv[-1].rsplit('@',1)[1]
if version=='2.3.0': sys.exit(1)
client=target/'node_modules'/'.bin'/'claude'
client.parent.mkdir(parents=True)
client.write_text('#!EXECUTABLE\\nprint("'+version+'")\\n')
client.chmod(0o700)
'''.replace("EXECUTABLE", sys.executable))
        npm.chmod(0o700)
        control.inspect()
        old = control.executable()
        control.state.update(latest_version="2.2.0", update_available=True)
        with patch("prediction_market_agent.plugins.providers._client_control.shutil.which", return_value=str(npm)):
            control.upgrade()
            control.update_thread.join(5)
            self.assertEqual(control.snapshot()["installed_version"], "2.2.0")
            self.assertTrue(Path(old).exists())
            active = control.executable()
            self.assertNotEqual(old, active)
            control.state.update(latest_version="2.3.0", update_available=True)
            control.upgrade()
            control.update_thread.join(5)
            self.assertEqual(control.snapshot()["update_state"], "failed")
            self.assertEqual(control.executable(), active)
        self.assertTrue(Path(active).exists())

    def test_model_and_private_configuration_fields_are_independent(self):
        for name, model in (("codex", "account-model-a"), ("claude", "account-model-b")):
            spec, control = self.plugin(name)
            spec.configuration.save({**spec.configuration.load(), f"{name.upper()}_MODEL": model})
            self.assertEqual(spec.factory(None).model, model)
            self.assertTrue(all(field.description for field in spec.configuration.fields))
