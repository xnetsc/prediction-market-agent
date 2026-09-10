"""Authenticated host-side gateway to one configured upstream proxy."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import secrets
import selectors
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

spec = importlib.util.spec_from_file_location("host_proxy", Path(__file__).with_name("host-proxy.py"))
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def start_gateway(upstream, bind):
    parsed = urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An HTTP/HTTPS upstream proxy is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((parsed.hostname, port), timeout=3):
        pass
    secret = secrets.token_urlsafe(32)
    expected = b"Basic " + base64.b64encode(("relay:" + secret).encode())
    authorization = None
    if parsed.username is not None:
        authorization = base64.b64encode((unquote(parsed.username) + ":" + unquote(parsed.password or "")).encode())
    slots = threading.BoundedSemaphore(32)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not slots.acquire(blocking=False):
                return
            try:
                self.request.settimeout(3)
                header = bytearray()
                deadline = time.monotonic() + 3
                while len(header) < 32768 and time.monotonic() < deadline and not header.endswith(b"\r\n\r\n"):
                    part = self.request.recv(1)
                    if not part:
                        return
                    header.extend(part)
                if not header.endswith(b"\r\n\r\n"):
                    return
                lines = bytes(header).split(b"\r\n")
                auth = [line.split(b":", 1)[1].strip() for line in lines[1:] if line.lower().startswith(b"proxy-authorization:")]
                if len(auth) != 1 or not secrets.compare_digest(auth[0], expected):
                    self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=relay\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                lines = [line for line in lines if not line.lower().startswith(b"proxy-authorization:")]
                if authorization:
                    lines.insert(1, b"Proxy-Authorization: Basic " + authorization)
                with socket.create_connection((parsed.hostname, port), timeout=5) as tcp:
                    remote = ssl.create_default_context().wrap_socket(tcp, server_hostname=parsed.hostname) if parsed.scheme == "https" else tcp
                    try:
                        remote.sendall(b"\r\n".join(lines))
                        self.request.settimeout(5)
                        with selectors.DefaultSelector() as selector:
                            selector.register(self.request, selectors.EVENT_READ, remote)
                            selector.register(remote, selectors.EVENT_READ, self.request)
                            deadline = time.monotonic() + 3600
                            while time.monotonic() < deadline:
                                for event, _ in selector.select(timeout=1):
                                    block = event.fileobj.recv(65536)
                                    if not block:
                                        return
                                    event.data.sendall(block)
                                    deadline = time.monotonic() + 3600
                    finally:
                        if remote is not tcp:
                            remote.close()
            except (OSError, ValueError):
                return
            finally:
                slots.release()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    server = Server((bind, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, secret


def container_running(container, docker_sudo=False):
    command = (["sudo", "-n"] if docker_sudo else []) + [
        "docker", "inspect", "--format", "{{.State.Running}}", container]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and result.stdout.strip() == "true"


def run(path, bind, container_host, ready_file=None, docker_sudo=False):
    snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    capture = snapshot["captured_at"]
    server, secret = start_gateway(snapshot["host_proxy"], bind)
    snapshot.update(proxy=f"http://relay:{secret}@{container_host}:{server.server_address[1]}",
                    status="proxy", error="", needs_forwarding=False, forwarded=True,
                    source=snapshot["source"] + " (host script forwarding)")
    storage.write_snapshot(path, snapshot)
    if ready_file:
        ready_file.write_text("ready\n")
    print("Host proxy forwarding ready (authenticated, upstream fixed).", flush=True)
    started = time.monotonic()
    try:
        while True:
            time.sleep(1)
            current = json.loads(path.read_text(encoding="utf-8-sig"))
            if current.get("captured_at") != capture:
                break
            container = current.get("container_id")
            if container:
                if not container_running(container, docker_sudo):
                    break
            elif time.monotonic() - started > 300:
                break
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--container-host", required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--docker-sudo", action="store_true")
    args = parser.parse_args()
    try:
        run(args.snapshot, args.bind, args.container_host, args.ready_file, args.docker_sudo)
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Host proxy forwarding failed; check host proxy connectivity/binding and restart the launcher") from None
