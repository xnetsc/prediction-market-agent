#!/usr/bin/env python3
"""Serve an upstream image registry on loopback, fetching through the host's proxy.

Image pulls are made by the Docker daemon, which reads neither the shell's proxy variables nor
anything this script could set for a single command. Every documented way to give the daemon a
proxy edits daemon configuration and restarts a service the operator may share with other work.

This takes the other route: the daemon is pointed at 127.0.0.1, which Docker already treats as an
insecure registry without any configuration, and this process performs the upstream requests using
the proxy that was detected on the host. Nothing about Docker is reconfigured, and the arrangement
lasts exactly as long as this process does.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request


UPSTREAM_TIMEOUT = 120
STREAM_CHUNK = 1 << 16

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.v1+prettyjws",
    )
)

PATH_PATTERN = re.compile(r"^/v2/(?P<name>[^\s]+)/(?P<kind>manifests|blobs)/(?P<reference>[^/\s]+)$")


class UpstreamError(RuntimeError):
    pass


class Upstream:
    """Anonymous registry client that talks through a fixed proxy."""

    def __init__(self, registry: str, proxy: str):
        self.registry = registry
        self.proxy = proxy
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()
        handler = (
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            if proxy
            else urllib.request.ProxyHandler({})
        )
        self._opener = urllib.request.build_opener(handler)

    def _token_from_challenge(self, header: str, name: str) -> str:
        """Exchange an auth challenge for a pull token, anonymously."""
        if not header.lower().startswith("bearer "):
            return ""
        fields = dict(re.findall(r'(\w+)="([^"]*)"', header[len("bearer ") :]))
        realm = fields.pop("realm", "")
        if not realm:
            return ""
        fields.setdefault("scope", f"repository:{name}:pull")
        query = urllib.parse.urlencode(fields)
        try:
            with self._opener.open(f"{realm}?{query}", timeout=UPSTREAM_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise UpstreamError(f"token request failed: {error}") from error
        return str(payload.get("token") or payload.get("access_token") or "")

    def _send(self, url: str, method: str, accept: str, token: str):
        headers = {
            "Accept": accept or MANIFEST_ACCEPT,
            "User-Agent": "prediction-market-agent/registry-forwarder",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            return self._opener.open(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as error:
            return error
        except (urllib.error.URLError, OSError) as error:
            raise UpstreamError(str(getattr(error, "reason", error))) from error

    def open(self, name: str, kind: str, reference: str, method: str, accept: str):
        """Fetch from upstream, learning the auth challenge from the request that needs it.

        Registries disagree about which endpoints answer a probe - GHCR rejects HEAD on tag
        listings outright - so the challenge is taken from the real request rather than from a
        guessed endpoint.
        """
        url = f"https://{self.registry}/v2/{name}/{kind}/{reference}"
        with self._lock:
            token = self._tokens.get(name, "")
        response = self._send(url, method, accept, token)
        if getattr(response, "code", None) != 401:
            return response
        challenge = response.headers.get("WWW-Authenticate", "")
        response.close()
        fresh = self._token_from_challenge(challenge, name)
        if not fresh:
            raise UpstreamError("registry requires credentials this forwarder does not have")
        with self._lock:
            self._tokens[name] = fresh
        return self._send(url, method, accept, fresh)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream: Upstream
    repository_prefix = ""

    def log_message(self, *_args) -> None:  # noqa: D401 - quiet by default
        """Stay silent; the caller's log is the interesting one."""

    def _reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self, method: str) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/v2", "/v2/"}:
            self._reply(200, b"{}")
            return
        match = PATH_PATTERN.match(path)
        if not match:
            self._reply(404, b'{"errors":[{"code":"UNSUPPORTED"}]}')
            return
        name = self.repository_prefix + match["name"] if self.repository_prefix else match["name"]
        try:
            response = self.upstream.open(
                name, match["kind"], match["reference"], method, self.headers.get("Accept", "")
            )
        except UpstreamError as error:
            message = str(error).replace('"', "'")
            self._reply(502, f'{{"errors":[{{"code":"UNAVAILABLE","message":"{message}"}}]}}'.encode())
            return
        status = getattr(response, "status", None) or response.getcode()
        headers = response.headers
        length = headers.get("Content-Length")
        self.send_response(status)
        for header in ("Content-Type", "Docker-Content-Digest", "Etag"):
            if headers.get(header):
                self.send_header(header, headers[header])
        self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
        if length is not None:
            self.send_header("Content-Length", length)
            self.end_headers()
            if method != "HEAD":
                self._stream(response)
        else:
            body = response.read() if method != "HEAD" else b""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        response.close()

    def _stream(self, response) -> None:
        while True:
            chunk = response.read(STREAM_CHUNK)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._proxy("GET")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._proxy("HEAD")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(registry: str, proxy: str, host: str, port: int, prefix: str) -> Server:
    handler = type(
        "BoundHandler", (Handler,), {"upstream": Upstream(registry, proxy), "repository_prefix": prefix}
    )
    server = Server((host, port), handler)
    threading.Thread(target=server.serve_forever, name="registry-forwarder", daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="ghcr.io", help="upstream registry host")
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", ""), help="proxy for upstream requests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port and prints it")
    parser.add_argument("--prefix", default="", help="prepended to the requested repository name")
    parser.add_argument("--ready-file", default="", help="write the listening address here once serving")
    arguments = parser.parse_args()

    server = serve(
        arguments.registry, arguments.proxy, arguments.host, arguments.port, arguments.prefix
    )
    address = f"{arguments.host}:{server.server_address[1]}"
    print(address, flush=True)
    if arguments.ready_file:
        with open(arguments.ready_file, "w", encoding="utf-8") as handle:
            handle.write(address + "\n")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
