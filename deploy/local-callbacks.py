"""Local Docker deployment transport; no credentials or OAuth decisions live here."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import selectors
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit


def active_ports(payload, now):
    result = {}
    for item in payload["items"]:
        status = item["status"]
        if (status.get("state") != "authorizing" or status.get("callback_mode") == "helper"
                or status.get("callback_pending") is False):
            continue
        callback = urlsplit(status.get("redirect_uri", ""))
        probe = status.get("callback_probe") or {}
        target = urlsplit(probe.get("url", ""))
        if (callback.scheme != "http" or callback.hostname not in {"localhost", "127.0.0.1"}
                or not callback.port or callback.port < 1024 or callback.username or callback.password
                or callback.query or callback.fragment or target.netloc != callback.netloc
                or target.scheme != callback.scheme or not probe.get("expected_proof")
                or not target.path.startswith("/.well-known/prediction-login/")):
            continue
        lifetime = min(86400, math.ceil(float(status.get("expires_at", 0)) - now))
        if lifetime > 0 and status.get("flow_id"):
            result[callback.port] = (status["flow_id"], lifetime)
    return result


def changes(previous, current):
    for port, (flow, _) in previous.items():
        if port not in current or current[port][0] != flow:
            yield f"REMOVE {port} 0"
    for port, (flow, lifetime) in current.items():
        if port not in previous or previous[port][0] != flow:
            yield f"ADD {port} {lifetime}"


def watch(api_port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    previous = {}
    ready = False
    while True:
        request = urllib.request.Request(f"http://127.0.0.1:{api_port}/api/local",
            data=b'{"url":"/api/plugins/controls"}', headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=10) as response:
                current = active_ports(json.load(response), time.time())
        except (OSError, ValueError, KeyError, TypeError):
            print("Waiting for local client status; no callback credentials are logged.", file=sys.stderr, flush=True)
        else:
            if not ready:
                print("READY 0 0", flush=True)
                ready = True
            for event in changes(previous, current):
                print(event, flush=True)
            previous = current
        time.sleep(1)


def tunnel(target, port, lifetime):
    address = ipaddress.ip_address(target)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError("Expected the target container's private network address")
    slots = threading.BoundedSemaphore(16)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not slots.acquire(blocking=False):
                return
            try:
                with socket.create_connection((target, port), timeout=5) as upstream:
                    self.request.settimeout(5)
                    upstream.settimeout(5)
                    with selectors.DefaultSelector() as selector:
                        selector.register(self.request, selectors.EVENT_READ, upstream)
                        selector.register(upstream, selectors.EVENT_READ, self.request)
                        deadline = time.monotonic() + 45
                        while time.monotonic() < deadline:
                            for key, _ in selector.select(timeout=1):
                                data = key.fileobj.recv(65536)
                                if not data:
                                    return
                                key.data.sendall(data)
            except OSError:
                # Never log raw HTTP: it contains the user's OAuth callback query.
                return
            finally:
                slots.release()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = False

    with Server(("0.0.0.0", port), Handler) as server:
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        server.timeout = 1
        deadline = time.monotonic() + lifetime
        while not stopped.is_set() and time.monotonic() < deadline:
            server.handle_request()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("watch", "tunnel"))
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--target")
    parser.add_argument("--port", type=int)
    parser.add_argument("--lifetime", type=int, default=600)
    args = parser.parse_args()
    if args.mode == "watch":
        if not 1 <= args.api_port <= 65535:
            parser.error("Invalid management port")
        watch(args.api_port)
    else:
        if not args.port or not 1024 <= args.port <= 65535 or not 1 <= args.lifetime <= 86400:
            parser.error("Invalid callback port or lifetime")
        tunnel(args.target, args.port, args.lifetime)
