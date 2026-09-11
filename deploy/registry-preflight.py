#!/usr/bin/env python3
"""Decide how this machine can reach the image registry, without changing Docker's configuration.

Image pulls are made by the Docker daemon, which does not read the shell's proxy variables. On a
network where the registry is only reachable through a proxy, a plain `docker pull` therefore
stalls even though the same proxy works perfectly from the shell. The usual advice is to put the
proxy into Docker's own settings, but that is a persistent change to a service the operator may
share with other work, so this script never makes it. It reports what each path can actually do and
leaves the choice to the caller.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


DEPLOYMENT = Path(__file__).resolve().parent

REGISTRY_PROBE_TIMEOUT = 8


def registry_host(image: str) -> str:
    """Registry hostname for an image reference, following Docker's own defaults."""
    head = image.split("/", 1)[0]
    if "/" in image and ("." in head or ":" in head or head == "localhost"):
        return head
    return "registry-1.docker.io"


def host_proxy() -> tuple[str, str, str]:
    """Ask the project's existing collector what proxy this host is configured with.

    Proxy detection already exists here, once per platform, in host-proxy.sh and host-proxy.ps1,
    and the parsing of what they emit already lives in host-proxy.py. Re-deriving any of it would
    create a second definition of the same thing that could disagree with the first, including on
    awkward cases such as PAC and WPAD, where there is no explicit endpoint to use.
    """
    collector = DEPLOYMENT / ("host-proxy.ps1" if platform.system() == "Windows" else "host-proxy.sh")
    command = (
        ["powershell", "-NoProfile", "-File", str(collector)]
        if platform.system() == "Windows"
        else ["sh", str(collector)]
    )
    try:
        emitted = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return "", "collector failed", str(error)
    if emitted.returncode:
        return "", "collector failed", emitted.stderr.strip()[:300]
    parser = _load_host_proxy()
    try:
        proxy, _bypass, source, error = parser.select(parser.frames(emitted.stdout))
    except (ValueError, TypeError) as failure:
        return "", "collector failed", str(failure)
    return proxy, source, error


def _load_host_proxy():
    spec = importlib.util.spec_from_file_location("host_proxy", DEPLOYMENT / "host-proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(host: str, proxy: str) -> dict[str, object]:
    """Ask the registry for its API root; 401 counts as reachable for an anonymous client."""
    url = f"https://{host}/v2/"
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url, headers={"User-Agent": "prediction-market-agent/preflight"})
    try:
        with opener.open(request, timeout=REGISTRY_PROBE_TIMEOUT) as response:
            return {"ok": True, "status": response.status}
    except urllib.error.HTTPError as error:
        return {"ok": error.code in {401, 403}, "status": error.code}
    except (urllib.error.URLError, socket.timeout, OSError) as error:
        reason = getattr(error, "reason", error)
        return {"ok": False, "status": None, "error": str(reason)}


def daemon_proxy() -> dict[str, str]:
    """What the Docker daemon itself is configured to use. Reported, never changed."""
    if not shutil.which("docker"):
        return {"available": "false"}
    try:
        output = subprocess.run(
            [
                "docker",
                "info",
                "--format",
                "{{.HTTPProxy}}\t{{.HTTPSProxy}}\t{{.NoProxy}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": "false", "error": str(error)}
    if output.returncode:
        return {"available": "false", "error": output.stderr.strip()[:300]}
    http, _, rest = output.stdout.strip().partition("\t")
    https, _, no_proxy = rest.partition("\t")
    return {
        "available": "true",
        "http_proxy": http.strip(),
        "https_proxy": https.strip(),
        "no_proxy": no_proxy.strip(),
    }


def decide(image: str, requested: str) -> dict[str, object]:
    host = registry_host(image)
    detected, source, detection_error = ("", "", "")
    if requested == "off":
        candidate = ""
        source = "disabled"
    elif requested == "auto":
        detected, source, detection_error = host_proxy()
        candidate = detected
    else:
        candidate, source = requested, "requested"

    direct = probe(host, "")
    through = probe(host, candidate) if candidate else {"ok": False, "skipped": True}
    daemon = daemon_proxy()
    daemon_has_proxy = bool(daemon.get("https_proxy") or daemon.get("http_proxy"))

    if direct["ok"]:
        action, reason = "proceed", "registry reachable without a proxy"
    elif candidate and through.get("ok") and daemon_has_proxy:
        action, reason = (
            "proceed",
            "registry needs a proxy and the Docker daemon already routes through one",
        )
    elif candidate and through.get("ok"):
        action, reason = (
            "proxy-pull",
            "the proxy reaches the registry but the Docker daemon is not using one",
        )
    elif candidate:
        action, reason = "ask", "a proxy is configured but it cannot reach the registry either"
    else:
        action, reason = "ask", detection_error or "the registry is unreachable and no proxy is configured"

    return {
        "image": image,
        "registry": host,
        "proxy": candidate,
        "proxy_source": source,
        "proxy_detection_error": detection_error,
        "direct": direct,
        "through_proxy": through,
        "daemon": daemon,
        "action": action,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument(
        "--proxy",
        default=os.environ.get("PREDICTION_AGENT_REGISTRY_PROXY", "auto"),
        help="auto (default), off, or an explicit http(s) proxy URL",
    )
    parser.add_argument("--json", action="store_true", help="emit the full decision as JSON")
    arguments = parser.parse_args()

    result = decide(arguments.image, str(arguments.proxy).strip() or "auto")
    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"registry={result['registry']}")
        print(f"action={result['action']}")
        print(f"proxy={result['proxy']}")
        print(f"reason={result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
