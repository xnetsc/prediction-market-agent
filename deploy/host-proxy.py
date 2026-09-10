"""Parse host proxy settings before application startup, using the image's Python."""
from __future__ import annotations

import argparse
import ast
import base64
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
import urllib.request


def frames(raw):
    result = {}
    for line in raw.splitlines():
        name, value = line.split("\t", 1)
        result[name] = base64.b64decode(value, validate=True).decode("utf-8")
    return result


def select(settings):
    bypass = settings.get("env_no", "")
    for name in ("env_https", "env_http", "env_all"):
        if settings.get(name):
            return settings[name], bypass, "environment", ""
    platform = settings.get("platform")
    if platform == "Darwin":
        raw = settings.get("mac", "")
        value = lambda key: (re.search(r"\b" + key + r"\s*:\s*(\S+)", raw) or [None, ""])[1]
        if value("ProxyAutoConfigEnable") == "1" or value("ProxyAutoDiscoveryEnable") == "1":
            return "", bypass, "macOS system", "PAC/WPAD requires an explicit HTTP proxy in the client settings"
        for kind in ("HTTPS", "HTTP"):
            if value(kind + "Enable") == "1":
                host, port = value(kind + "Proxy"), value(kind + "Port")
                if ":" in host and not host.startswith("["):
                    host = "[" + host + "]"
                exceptions = re.search(r"ExceptionsList\s*:.*?\{(.*?)\}", raw, re.S)
                if exceptions and not bypass:
                    bypass = ",".join(re.findall(r"\d+\s*:\s*(\S+)", exceptions[1]))
                return f"http://{host}:{port}", bypass, "macOS system", ""
        if value("SOCKSEnable") == "1":
            return "", bypass, "macOS system", "Only SOCKS was found; configure an HTTP/HTTPS proxy"
    elif platform == "Windows":
        values = json.loads(settings.get("windows", "{}"))
        if values.get("pac") or values.get("auto_detect"):
            return "", bypass, "Windows Internet Settings", "PAC/WPAD requires an explicit HTTP proxy in the client settings"
        if values.get("enabled"):
            server = values.get("server", "")
            entries = dict(part.split("=", 1) for part in server.split(";") if "=" in part)
            proxy = entries.get("https") or entries.get("http") or (server if not entries else "")
            if not proxy:
                return "", bypass, "Windows Internet Settings", "No HTTP/HTTPS proxy was found"
            if "://" not in proxy:
                proxy = "http://" + proxy
            return proxy, bypass or values.get("bypass", "").replace(";", ","), "Windows Internet Settings", ""
    elif platform == "Linux":
        values = {}
        for line in settings.get("environment", "").splitlines():
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip().strip("'\"")
        for key in ("https_proxy", "http_proxy", "all_proxy"):
            if values.get(key):
                return values[key], bypass or values.get("no_proxy", ""), "/etc/environment", ""
        gnome = {}
        for line in settings.get("gnome", "").splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3:
                try:
                    gnome[parts[0] + "." + parts[1]] = ast.literal_eval(parts[2])
                except (ValueError, SyntaxError):
                    gnome[parts[0] + "." + parts[1]] = parts[2]
        mode = gnome.get("org.gnome.system.proxy.mode")
        if mode == "auto":
            return "", bypass, "GNOME", "PAC requires an explicit HTTP proxy in the client settings"
        if mode == "manual":
            for kind in ("https", "http"):
                prefix = "org.gnome.system.proxy." + kind + "."
                host = gnome.get(prefix + "host")
                if host:
                    if ":" in host and not host.startswith("["):
                        host = "[" + host + "]"
                    # Do not silently discard credentials enabled in GNOME's HTTP proxy.
                    if str(gnome.get("org.gnome.system.proxy.http.use-authentication", "")).lower() == "true":
                        return "", bypass, "GNOME", "Authenticated GNOME proxy requires an explicit URL in the client settings"
                    bypass = bypass or ",".join(gnome.get("org.gnome.system.proxy.ignore-hosts", []))
                    return f"http://{host}:{gnome.get(prefix + 'port', 0)}", bypass, "GNOME", ""
            return "", bypass, "GNOME", "Manual proxy is enabled but has no HTTP/HTTPS endpoint"
        kde = settings.get("kde_ProxyType", "")
        if kde in {"2", "3"}:
            return "", bypass, "KDE", "PAC/WPAD requires an explicit HTTP proxy in the client settings"
        if kde == "1":
            proxy = settings.get("kde_httpsProxy") or settings.get("kde_httpProxy") or ""
            proxy = re.sub(r"\s+(\d+)$", r":\1", proxy)
            return proxy, bypass or settings.get("kde_NoProxyFor", ""), "KDE", "" if proxy else "Missing KDE proxy endpoint"
    elif platform not in {"Darwin", "Windows", "Linux"}:
        return "", bypass, "unknown", "Unsupported host operating system"
    return "", bypass, "no system proxy detected", ""


def snapshot(settings, *, check=True):
    proxy, bypass, source, error = select(settings)
    result = {"version": 1, "source": source, "proxy": "", "host_proxy": proxy, "no_proxy": bypass,
              "platform": settings.get("platform"), "needs_forwarding": False,
              "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "error" if error else "direct", "error": error}
    if error or not proxy:
        return result
    try:
        url = urlsplit(proxy)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.query or url.fragment
                or url.path not in {"", "/"} or any(c.isspace() for c in proxy)):
            raise ValueError("Invalid proxy")
        host = url.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        port = url.port or (443 if url.scheme == "https" else 80)
        if loopback:
            host = "host.proxy.internal" if settings["platform"] == "Linux" else "host.docker.internal"
            credentials = url.netloc.rsplit("@", 1)[0] + "@" if "@" in url.netloc else ""
            proxy = urlunsplit((url.scheme, credentials + host + ":" + str(port), "", "", ""))
        result.update(proxy=proxy, status="proxy")
        if check:
            with socket.create_connection((host, port), timeout=3):
                pass
    except ValueError:
        result.update(status="error", error="Invalid or unsupported proxy URL; use an HTTP/HTTPS proxy")
    except OSError:
        result.update(status="error", needs_forwarding=True, error="Proxy unreachable from container; host forwarding is required")
    return result


def verify_proxy_request(result, test_url="https://api.ipify.org?format=json"):
    """Prove the final proxy path with a real HTTPS request from the container."""
    if result.get("status") != "proxy":
        return result
    target = urlsplit(test_url)
    if target.scheme != "https" or not target.hostname or target.username or target.password:
        result.update(status="error", error="Proxy validation URL must be a public HTTPS URL")
        return result
    proxy = str(result.get("proxy", ""))
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        request = urllib.request.Request(
            test_url,
            headers={"User-Agent": "prediction-market-agent-proxy-check/1"},
        )
        with opener.open(request, timeout=8) as response:
            if not 200 <= response.status < 400:
                raise OSError("unexpected HTTP status")
            response.read(256)
        result["validated_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        result["validation_target"] = target.hostname
        result["error"] = ""
    except Exception:
        result.update(
            status="error",
            error=(
                "Detected proxy failed a real HTTPS request from the container; "
                "choose direct access or exit"
            ),
        )
    return result


def force_direct(result):
    result.update(
        source="user selected direct access after proxy validation failed",
        proxy="",
        status="direct",
        needs_forwarding=False,
        error="",
        validated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        validation_target="DIRECT",
    )
    return result


def write_snapshot(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".host-proxy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attach-container")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--force-direct", action="store_true")
    parser.add_argument(
        "--test-url",
        default="https://api.ipify.org?format=json",
        help="Public HTTPS URL used only to validate the detected proxy path",
    )
    args = parser.parse_args()
    try:
        if args.force_direct:
            result = force_direct(json.loads(args.output.read_text()))
        elif args.verify:
            result = json.loads(args.output.read_text())
            if result.get("status") == "proxy":
                proxy = urlsplit(result["proxy"])
                try:
                    with socket.create_connection((proxy.hostname, proxy.port or 80), timeout=3):
                        pass
                except OSError:
                    result.update(status="error", error="Forwarding address is still unreachable from the container")
                else:
                    result = verify_proxy_request(result, args.test_url)
        elif args.attach_container:
            result = json.loads(args.output.read_text())
            result["container_id"] = args.attach_container
        else:
            raw = sys.stdin.read(131073)
            settings = frames(raw)
            if len(raw) > 131072 or settings.get("complete") != "yes":
                raise ValueError("Incomplete proxy capture")
            result = snapshot(settings)
        write_snapshot(args.output, result)
    except (ValueError, OSError):
        raise SystemExit("Unable to capture system proxy; application was not started") from None
    print("Host proxy: " + result["source"] + " / " + result["status"])
    if result["error"]:
        print(result["error"] + "; client HOST mode will report an error, not silently connect directly.")
    if result.get("needs_forwarding"):
        print("Forwarding required: yes")
    if result.get("status") == "error":
        print("Proxy validation failed: yes")
