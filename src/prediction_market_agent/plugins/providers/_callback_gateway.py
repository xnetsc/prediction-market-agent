"""Per-login HTTP gateway proving that a published port reaches this process."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class CallbackGateway:
    def __init__(self, relay, bind_host: str, origin: str):
        self.relay = relay
        self.origin = origin
        self.path = "/.well-known/prediction-login/" + secrets.token_urlsafe(24)
        self.proof = secrets.token_urlsafe(32)
        self.observed = False
        self.closed = False
        self.close_lock = threading.Lock()
        callback = urlsplit(relay.redirect_uri)
        self.url = f"{callback.scheme}://{callback.netloc}{self.path}"
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Access-Control-Allow-Origin", gateway.origin)
                self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                if urlsplit(self.path).path != gateway.path or self.headers.get("Origin") != gateway.origin:
                    self.reply(403, {"error": "Origin or probe mismatch"})
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", gateway.origin)
                self.send_header("Access-Control-Allow-Methods", "GET")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")
                self.end_headers()

            def do_GET(self):
                with relay.lock:
                    active = not relay.closed and not relay.consumed
                if not active:
                    self.reply(410, {"error": "Login flow ended"})
                elif urlsplit(self.path).path == gateway.path:
                    if self.headers.get("Origin") != gateway.origin:
                        self.reply(403, {"error": "Origin mismatch"})
                        return
                    values = parse_qs(urlsplit(self.path).query)
                    challenge = values.get("challenge", [])
                    if (set(values) != {"challenge"} or len(challenge) != 1
                            or not re.fullmatch(r"[a-f0-9]{64}", challenge[0])):
                        self.reply(400, {"error": "A fresh probe challenge is required"})
                        return
                    gateway.observed = True
                    self.reply(200, {"proof": gateway.proof, "redirect_uri": relay.redirect_uri,
                                     "challenge": challenge[0]})
                elif urlsplit(self.path).path == callback.path:
                    try:
                        try:
                            relay.forward_callback(urlsplit(self.path).query)
                        except (ValueError, OSError):
                            self.reply(400, {"error": "Callback rejected; restart login in the dashboard"})
                        else:
                            self.reply(200, {"message": "Authorization received. Return to the dashboard."})
                            self.wfile.flush()
                    finally:
                        if relay.consumed:
                            gateway.close()
                else:
                    self.reply(404, {"error": "Unknown login route"})

            def log_message(self, *_):
                # OAuth callback queries must not enter access logs.
                return

        self.server = ThreadingHTTPServer((bind_host, callback.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.expiry_timer = threading.Timer(max(0, relay.expires_at - time.time()), self.close)
        self.expiry_timer.daemon = True
        self.expiry_timer.start()

    def status(self) -> dict:
        return {"url": self.url, "expected_proof": self.proof}

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
        self.expiry_timer.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
