"""Inspect isolated official login launches; never sign in or read host credentials."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.providers import claude, codex


def probe(name: str, serve_port: int):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        module = codex if name == "codex" else claude
        spec = module.initialize_plugin(PluginInitializationContext("decision_provider", Path(module.__file__), root))
        control = spec.controls.status_callback.__self__
        spec.configuration.save({name.upper() + "_CHECK_UPDATES": False})
        origin = f"http://127.0.0.1:{serve_port}" if serve_port else "http://localhost:8765"
        try:
            control.action("login", {"_public_origin": origin})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if control.relay and control.relay.redirect_uri:
                    break
                if control.process is None:
                    raise RuntimeError(name + ": login process exited before providing a callback")
                time.sleep(.1)
            else:
                raise RuntimeError(name + ": no official callback captured")
            status = control.snapshot()
            print(json.dumps({"client": name, "redirect_uri": status["redirect_uri"],
                              "gateway_created": bool(status["callback_probe"]),
                              "gateway_error": status["callback_probe_error"]}), flush=True)
            if not serve_port:
                return
            script = Path(__file__).resolve().parents[1].joinpath(
                "src/prediction_market_agent/runtime/static/login-probe.js").read_text()
            probe_config = json.dumps({"redirect": status["redirect_uri"], "probe": status["callback_probe"]})
            page = ("<!doctype html><meta charset='utf-8'><h1>Container callback mapping test</h1>"
                    "<button onclick='check()'>Verify actual callback mapping</button><p id='result'>Not tested</p>"
                    "<script>" + script + "\nconst config=" + probe_config + ";"
                    "async function check(){let result=document.getElementById('result');"
                    "result.textContent='Checking...';try{let proof=config.probe&&await probeLoginCallback(config.redirect,config.probe);"
                    "result.textContent=proof?'Verified: this container and this login flow':'Unverified: helper required';}"
                    "catch(e){result.textContent='Unverified: '+e.message}}</script>").encode()
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                def log_message(self, *_):
                    return
            server = HTTPServer(("0.0.0.0", serve_port), Handler)
            timer = threading.Timer(120, server.shutdown)
            timer.start()
            print("Browser probe page: " + origin, flush=True)
            try:
                server.serve_forever()
            finally:
                timer.cancel()
                server.server_close()
        finally:
            spec.teardown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude", "both"), default="both")
    parser.add_argument("--serve-port", type=int, default=0)
    args = parser.parse_args()
    if args.serve_port and args.client == "both":
        parser.error("Choose one client when serving a browser probe page")
    for client in ("codex", "claude") if args.client == "both" else (args.client,):
        probe(client, args.serve_port)
