"""Loopback-only login relay embedded into the Bash terminal script."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange(config: dict, payload: dict) -> dict:
    key = hashlib.sha256(config["secret"].encode()).digest()
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), b"helper-request")
    request = urllib.request.Request(config["endpoint"], data=json.dumps({
        "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(cipher).decode()}).encode(),
        headers={"Authorization": "Bearer " + config["secret"], "Content-Type": "application/json"})
    # Use OS proxy settings for this computer; never inherit the server's container proxy address.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=40) as response:
        envelope = json.loads(response.read(65536))
    return json.loads(AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]),
        base64.b64decode(envelope["ciphertext"]), b"helper-response"))


def run(config: dict):
    endpoint = urlsplit(config["endpoint"])
    if (endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1"})) or endpoint.username or endpoint.password:
        raise ValueError("Remote management endpoint must use HTTPS")
    if not endpoint.path.startswith("/api/plugin-helper/"):
        raise ValueError("Invalid helper endpoint")
    print("Login relay connects only to: " + endpoint.netloc, flush=True)
    print("Return to the login wizard after this window says Ready. No password or code is needed here.", flush=True)
    deadline = min(float(config["expires_at"]), time.time() + 900)
    while time.time() < deadline:
        state = exchange(config, {"action": "poll"})
        if state.get("completed"):
            return
        if state.get("redirect_uri"):
            break
        time.sleep(1)
    else:
        raise ValueError("Pairing expired; restart from the web login wizard")
    callback = urlsplit(state["redirect_uri"])
    if (callback.scheme != "http" or callback.hostname not in {"localhost", "127.0.0.1"}
            or callback.path not in {"/callback", "/auth/callback"} or not callback.port):
        raise ValueError("Invalid local callback address")
    received = False
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_GET(self):
            nonlocal received
            parts = urlsplit(self.path)
            if parts.path != callback.path or self.headers.get("Host") not in {f"localhost:{callback.port}", f"127.0.0.1:{callback.port}"}:
                self.send_error(404)
                return
            if len(parts.query) > 16384 or not parse_qs(parts.query).get("state"):
                self.send_error(400)
                return
            try:
                exchange(config, {"action": "callback", "query": parts.query})
                received = True
                content = b"<!doctype html><meta charset=utf-8><title>Login callback delivered</title><p>Authorization delivered. Return to the robot login wizard to check the result.</p>"
                self.send_response(200)
            except Exception:
                content = b"Callback could not be delivered. Return to the login wizard and restart."
                self.send_response(502)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_):
            pass  # OAuth query strings must not enter terminal or access logs.
    try:
        server = HTTPServer(("127.0.0.1", callback.port), Handler)
    except OSError:
        exchange(config, {"action": "error"})
        raise ValueError("Callback port is in use; close another login flow and restart") from None
    with server:
        server.timeout = 1
        exchange(config, {"action": "ready", "redirect_uri": state["redirect_uri"]})
        print("Ready. Open the official login page from the web wizard.", flush=True)
        while not received and time.time() < deadline:
            server.handle_request()
            if not received:
                status = exchange(config, {"action": "poll"})
                if status.get("completed") or status.get("redirect_uri") != state["redirect_uri"]:
                    raise ValueError("Login flow ended; return to the web login wizard")
    if not received:
        raise ValueError("Login timed out; restart from the web wizard")
    print("Callback delivered. Confirm login success in the web wizard.", flush=True)
