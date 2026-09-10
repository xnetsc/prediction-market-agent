"""Read-only runtime information and explicit, credential-isolated egress probes."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit, urlunsplit

from .. import __version__
from ..plugin_system.managed_config import PLUGIN_KINDS
from ..plugin_system.config_io import resolve_proxy_settings
from ..plugin_system.network_diagnostics import DiagnosticNetworkRoute


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def proxy_display(value):
    if not value:
        return "DIRECT"
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    return urlunsplit((parsed.scheme, host + (":" + str(parsed.port) if parsed.port else ""), "", "", ""))


def services_from(raw):
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("出口查询服务必须是最多 8 项的 JSON 数组")
    for item in values:
        if not isinstance(item, dict) or set(item) - {"name", "url", "ip_field", "port_field"}:
            raise ValueError("查询服务字段应为 name/url/ip_field/port_field")
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("name", "url", "ip_field")):
            raise ValueError("每个查询服务必须提供 name、url 和 ip_field")
        parsed = urlsplit(item["url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("公网查询服务必须使用无凭据的 HTTPS URL")
        if "port_field" in item and not isinstance(item["port_field"], str):
            raise ValueError("port_field 必须是字段名文本")
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RouteProxy(urllib.request.ProxyHandler):
    """Use the route's bypass list, never unrelated process proxy variables."""
    def __init__(self, route):
        self.route = route
        super().__init__({"https": route.proxy} if route.proxy else {})

    def proxy_open(self, req, proxy, type_):
        if urllib.request.proxy_bypass_environment(req.host, {"no": self.route.no_proxy}):
            return None
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Unsupported proxy")
        if parsed.username is not None:
            credentials = unquote(parsed.username) + ":" + unquote(parsed.password or "")
            req.add_unredirected_header("Proxy-Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        req.set_proxy(host + (":" + str(parsed.port) if parsed.port else ""), parsed.scheme)
        return None


def probe_service(service, route, timeout):
    started = time.monotonic()
    result = {"service": service["name"], "url": service["url"], "checked_at": utc_now(),
              "status": "error", "ip": None, "family": None, "observed_source_port": None}
    try:
        request = urllib.request.Request(service["url"], headers={
            "Accept": "application/json", "User-Agent": "PredictionAgent-NetworkDiagnostics/1", "Cache-Control": "no-cache"})
        opener = urllib.request.build_opener(RouteProxy(route), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = b""
            while len(raw) <= 16384:
                if time.monotonic()-started > timeout:
                    raise TimeoutError("Response deadline exceeded")
                chunk = response.read1(16385-len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > 16384:
                raise ValueError("Response too large")
            value = json.loads(raw)
        address = ipaddress.ip_address(value[service["ip_field"]])
        if not address.is_global:
            raise ValueError("Not a public address")
        port = value.get(service.get("port_field", ""))
        if port is not None:
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError("Invalid source port")
        result.update(status="ok", ip=str(address), family="IPv"+str(address.version), observed_source_port=port)
    except urllib.error.HTTPError as error:
        result["error"] = f"HTTP {error.code}（未跟随重定向）"
        error.close()
    except Exception as error:
        # Exceptions may contain a proxy password or remote response. Never expose them.
        result["error"] = "查询失败：" + type(error).__name__ + "；请检查网络、代理或服务字段"
    result["elapsed_ms"] = round((time.monotonic()-started)*1000)
    return result


class EnvironmentDiagnostics:
    def __init__(self, config, management, settings):
        self.config, self.management, self.settings = config, management, settings
        self.started_at, self.started = utc_now(), time.monotonic()
        self.cache = {}
        self.lock = threading.Lock()

    def routes(self):
        items = {"direct": ("服务器直连（不使用应用代理）", DiagnosticNetworkRoute("直连"), "")}
        host_proxy_file = Path(self.config.host_proxy_file).expanduser()
        if not host_proxy_file.is_absolute():
            host_proxy_file = Path(self.config.working_directory) / host_proxy_file
        try:
            inherited = resolve_proxy_settings(
                "INHERIT",
                field_name="统一继承代理",
                inherited_value=self.config.shared_http_proxy,
                inherited_no_proxy=self.config.shared_no_proxy,
                snapshot_file=host_proxy_file.resolve(),
            )
            items["inherited"] = (
                "统一继承代理（插件选择 INHERIT 时使用）",
                DiagnosticNetworkRoute(
                    "统一继承代理", inherited["proxy"], inherited["no_proxy"]
                ),
                "",
            )
        except Exception:
            items["inherited"] = (
                "统一继承代理（插件选择 INHERIT 时使用）",
                None,
                "统一代理未就绪；请重新运行一键启动器或修改统一代理设置",
            )
        try:
            values = urllib.request.getproxies()
            items["environment"] = ("服务器环境代理", DiagnosticNetworkRoute("运行环境",
                values.get("https") or values.get("http") or values.get("all") or "",values.get("no", "")), "")
        except Exception:
            items["environment"] = ("服务器环境代理", None, "运行环境代理无法解析")
        for kind in PLUGIN_KINDS:
            for spec in self.management.catalog.specs(kind):
                key = kind+":"+spec.name
                if spec.network_routes_callback is None:
                    continue
                try:
                    routes = spec.network_routes_callback()
                    for index, route in enumerate(routes):
                        if not isinstance(route, DiagnosticNetworkRoute):
                            raise ValueError("Invalid network route")
                        items[key+":"+str(index)] = (key+" · "+route.label, route, "")
                except Exception:
                    items[key+":0"] = (key, None, "插件网络配置未就绪；请配置该插件的代理字段")
        return items

    def fingerprint(self, route):
        settings = self.settings.values()
        return hashlib.sha256(json.dumps([route.proxy,route.no_proxy,settings["environment_probe_services"],
                    settings["environment_probe_timeout"]]).encode()).hexdigest()

    def snapshot(self, request):
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(),None)})
        except OSError:
            addresses = []
        try:
            disk = shutil.disk_usage(self.config.working_directory)
            disk_info = {"total_bytes":disk.total,"free_bytes":disk.free}
        except OSError:
            disk_info = {"error":"工作目录磁盘信息不可用"}
        routes = []
        for key,(label,route,error) in self.routes().items():
            cached = self.cache.get(key)
            routes.append({"id":key,"label":label,"proxy":proxy_display(route.proxy) if route else None,
                "no_proxy":route.no_proxy if route else "", "error":error,
                "result":cached[1] if cached else None,
                "stale":bool(cached and (route is None or cached[0]!=self.fingerprint(route)))})
        files = {}
        for name in ("session_db","auth_db","application_config_file"):
            path = Path(getattr(self.config,name)).resolve()
            try:
                files[name] = {"path":str(path),"exists":path.exists(),"bytes":path.stat().st_size if path.exists() else None}
            except OSError:
                files[name] = {"path":str(path),"error":"文件状态不可读"}
        return {"sampled_at":utc_now(),"app_version":__version__,"python":platform.python_version(),
            "os":platform.system(),"os_release":platform.release(),"architecture":platform.machine(),
            "hostname":socket.gethostname(),"pid":os.getpid(),"cpu_count":os.cpu_count(),
            "container_marker":Path('/.dockerenv').exists() or Path('/run/.containerenv').exists(),
            "started_at":self.started_at,"uptime_seconds":round(time.monotonic()-self.started),
            "working_directory":str(self.config.working_directory.resolve()),"files":files,"disk":disk_info,
            "hostname_addresses":addresses,"request_peer":request.client.host if request.client else None,
            "asgi_server":request.scope.get('server'),"request_origin":str(request.base_url),"routes":routes}

    def probe(self, route_id):
        if not self.lock.acquire(blocking=False):
            raise ValueError("已有出口查询正在进行，请等待完成")
        try:
            entry = self.routes().get(route_id)
            if entry is None or entry[1] is None:
                raise ValueError("所选网络路径不存在或配置未就绪")
            route = entry[1]
            settings = self.settings.values()
            services = services_from(settings["environment_probe_services"])
            fingerprint = self.fingerprint(route)
            if not services:
                results = []
            else:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(lambda s:probe_service(s,route,settings["environment_probe_timeout"]),services))
            ips = sorted({result['ip'] for result in results if result['status']=='ok'})
            families = {family:{r['ip'] for r in results if r['status']=='ok' and r['family']==family} for family in ('IPv4','IPv6')}
            value = {"checked_at":utc_now(),"observations":results,"ips":ips,
                "status":"disabled" if not services else "ok" if len(results)==sum(r['status']=='ok' for r in results) else "partial" if ips else "error",
                "same_family_disagreement":any(len(addresses)>1 for addresses in families.values())}
            self.cache[route_id] = (fingerprint,value)
            return value
        finally:
            self.lock.release()
