from __future__ import annotations

import json
import secrets
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.plugins.providers._login_relay import BrowserLoginRelay


class CallbackMappingTests(unittest.TestCase):
    def relay(self, timeout=60):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        relay = BrowserLoginRelay(timeout, bind_host="127.0.0.1", origin="http://localhost:8765")
        self.addCleanup(relay.close)
        self.assertTrue(relay.configure(
            f"https://auth.openai.com/authorize?state=fixture&redirect_uri=http://127.0.0.1:{port}/auth/callback"))
        self.assertIsNotNone(relay.gateway)
        return relay

    def read_probe(self, relay, origin="http://localhost:8765"):
        request = urllib.request.Request(relay.gateway.url + "?challenge=" + secrets.token_hex(32),
                                         headers={"Origin": origin})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=3)

    def test_matching_mapping_requires_observed_request_and_flow_proof(self):
        relay = self.relay()
        with self.assertRaisesRegex(ValueError, "尚未验证"):
            relay.direct_ready(relay.redirect_uri, relay.gateway.proof)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.read_probe(relay, "https://other.example")
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(relay.gateway.observed)
        with self.read_probe(relay) as response:
            proof = json.load(response)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:8765")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.assertRaises(ValueError):
            relay.direct_ready(relay.redirect_uri, "another-login-proof")
        with self.assertRaises(ValueError):
            relay.direct_ready("http://localhost:1234/auth/callback", proof["proof"])
        relay.direct_ready(relay.redirect_uri, proof["proof"])
        self.assertEqual(relay.status()["callback_mode"], "direct")
        self.assertTrue(relay.status()["authorization_url"])
        self.assertFalse(relay.consumed)
        relay.close()
        with self.assertRaises(ValueError):
            relay.direct_ready(relay.redirect_uri, proof["proof"])
        self.assertIsNone(relay.status()["callback_probe"])

    def test_occupied_container_port_and_missing_mapping_cannot_be_approved(self):
        first = self.relay()
        second = BrowserLoginRelay(60, bind_host="127.0.0.1", origin=first.origin)
        self.addCleanup(second.close)
        self.assertTrue(second.configure(first.authorization_url))
        self.assertIsNone(second.gateway)
        self.assertIn("无法监听", second.status()["callback_probe_error"])
        with self.assertRaises(ValueError):
            second.direct_ready(second.redirect_uri, first.gateway.proof)
        plain = BrowserLoginRelay(60)
        plain.configure(first.authorization_url)
        with self.assertRaises(ValueError):
            plain.direct_ready(plain.redirect_uri, first.gateway.proof)

    def test_instances_and_login_flows_have_independent_random_proofs(self):
        first, second = self.relay(), self.relay()
        self.assertNotEqual(first.gateway.path, second.gateway.path)
        self.assertNotEqual(first.gateway.proof, second.gateway.proof)
        with self.read_probe(first) as response:
            one = json.load(response)
        with self.read_probe(first) as response:
            another = json.load(response)
        with self.read_probe(second) as response:
            two = json.load(response)
        self.assertNotEqual(one["challenge"], another["challenge"])
        self.assertNotEqual(one["proof"], two["proof"])
        with self.assertRaises(ValueError):
            second.direct_ready(second.redirect_uri, one["proof"])
        second.direct_ready(second.redirect_uri, two["proof"])

    def test_missing_or_malformed_probe_challenge_does_not_verify_a_listener(self):
        relay = self.relay()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for query in ("", "?challenge=fixed", "?challenge=" + "a" * 64 + "&challenge=" + "b" * 64):
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(urllib.request.Request(relay.gateway.url + query, headers={"Origin": relay.origin}), timeout=3)
            self.assertEqual(error.exception.code, 400)
            self.assertFalse(relay.gateway.observed)

    def test_expired_proof_does_not_enable_direct_login(self):
        relay = self.relay()
        with self.read_probe(relay) as response:
            proof = json.load(response)
        with patch("prediction_market_agent.plugins.providers._login_relay.time.time",
                   return_value=relay.expires_at + 1):
            with self.assertRaises(ValueError):
                relay.direct_ready(relay.redirect_uri, proof["proof"])

    def test_browser_probe_rejects_foreign_listener_and_only_accepts_matching_proof(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run(["node", "--test", str(root / "tests" / "login_probe.test.cjs")],
                       cwd=root, check=True, capture_output=True, text=True)

    def assert_listener_closed(self, port):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with socket.socket() as connection:
                connection.settimeout(.1)
                if connection.connect_ex(("127.0.0.1", port)) != 0:
                    return
            time.sleep(.02)
        self.fail("Callback listener remained open")

    def test_callback_response_is_delivered_then_listener_closes_without_waiting_for_cli(self):
        relay = self.relay()
        gateway = relay.gateway
        def received(query):
            relay.consumed = True
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(relay, "forward_callback", side_effect=received):
            with opener.open(relay.redirect_uri + "?state=fixture&code=fixture", timeout=3) as response:
                self.assertEqual(json.load(response)["message"], "Authorization received. Return to the dashboard.")
        self.assert_listener_closed(gateway.server.server_port)
        self.assertFalse(relay.status()["callback_pending"])
        self.assertIsNone(relay.status()["callback_probe"])

    def test_expiry_and_explicit_close_release_the_actual_port(self):
        for expires in (False, True):
            with self.subTest(expires=expires):
                relay = self.relay(timeout=.1 if expires else 60)
                port = relay.gateway.server.server_port
                if not expires:
                    relay.close()
                    relay.close()
                self.assert_listener_closed(port)
                self.assertFalse(relay.status()["callback_pending"])
