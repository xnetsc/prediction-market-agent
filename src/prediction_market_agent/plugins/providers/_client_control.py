"""Persistent CLI accounts and versioned client installs, owned by provider plugins."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import shlex
import socket
from ipaddress import ip_address
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginControls
from prediction_market_agent.plugin_system.managed_config import atomic_write_text
from ._shared import resolve_executable, subprocess_environment
from ._login_relay import BrowserLoginRelay
from ._host_proxy import client_proxy
from prediction_market_agent.plugin_system.network_diagnostics import DiagnosticNetworkRoute


CREDENTIAL_BUNDLE_KIND = "prediction-market-agent/client-credentials"
CREDENTIAL_BUNDLE_VERSION = 1

CREDENTIAL_FILES = {
    "codex": (".codex/auth.json",),
    "claude": (".claude/.credentials.json",),
}
"""Files that restore a signed-in session, per client.

Deliberately narrow: the client home also accumulates local settings, project history and logs,
none of which is needed to stay logged in and some of which an operator would not expect to leave
the machine inside a credential file.
"""

CREDENTIAL_FILE_MAX_BYTES = 1_048_576

def client_configuration_loader(load, prefix: str):
    """Read pre-script configurations without altering credentials or private files."""
    def compatible_load():
        values = dict(load())
        values.pop(f"{prefix}_HELPER_DIRECTORY", None)
        return values
    return compatible_load


def client_fields(prefix: str) -> tuple[PluginConfigField, ...]:
    return (
        PluginConfigField(f"{prefix}_HOST_PROXY_FILE", "宿主机代理快照文件", "string",
                          "HOST 优先读取此快照，不存在时检测当前运行环境；ENVIRONMENT 忽略快照。相对机器人工作目录，可能含代理凭据。",
                          default=".deployment/host-proxy.json"),
        PluginConfigField(f"{prefix}_CALLBACK_BIND_HOST", "回调映射验证监听地址", "string",
                          "AUTO 在 Docker 中使用容器网卡地址，以官方回调原端口提供本次流程验证与转发；仍需 Docker 同端口映射。留空关闭验证入口并使用助手，不修改官方 redirect_uri。",
                          default="AUTO"),
        PluginConfigField(f"{prefix}_AUTH_DIRECTORY", "登录凭据目录", "string",
                          "客户端独立 HOME；相对机器人工作目录解析。应位于持久数据卷，切换目录不搬迁或删除旧凭据。",
                          default=f"credentials/{prefix.lower()}"),
        PluginConfigField(f"{prefix}_CLIENT_DIRECTORY", "客户端升级目录", "string",
                          "保存版本化安装及当前版本指针；相对工作目录解析，不覆盖镜像自带客户端。",
                          default=f"clients/{prefix.lower()}"),
        PluginConfigField(f"{prefix}_CHECK_UPDATES", "自动检查新版", "boolean",
                          "后台只检查官方 npm 最新版本，不自动安装；升级必须在界面点击。", default=True),
        PluginConfigField(f"{prefix}_UPDATE_CHECK_SECONDS", "检查新版间隔（秒）", "integer",
                          "后台版本检查间隔，至少 60 秒；默认六小时。", default=21600),
        PluginConfigField(f"{prefix}_LOGIN_TIMEOUT_SECONDS", "登录等待期限（秒）", "integer",
                          "官方授权流程超时后终止等待，用户可重新点击登录。", default=600),
    )


class ClientControl:
    def __init__(
        self,
        name: str,
        package: str,
        configuration,
        working_directory: Path,
        proxy_resolver=None,
    ):
        self.name, self.package = name, package
        self.prefix = name.upper()
        self.configuration, self.root = configuration, working_directory
        self.proxy_resolver = proxy_resolver
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.process: subprocess.Popen | None = None
        self.install_process: subprocess.Popen | None = None
        self.login_thread: threading.Thread | None = None
        self.update_thread: threading.Thread | None = None
        self.monitor: threading.Thread | None = None
        self.state: dict[str, Any] = {"state": "checking", "message": "正在检查客户端登录状态",
                                      "installed_version": "", "latest_version": "",
                                      "update_state": "idle", "update_available": False}
        self.last_update_check = 0.0
        self.flow_id = ""
        self.closed = False
        self.inspection_lock = threading.Lock()
        self.relay: BrowserLoginRelay | None = None
        self.browser_thread: threading.Thread | None = None
        self.controls = PluginControls(self.snapshot, self.action, self.helper_message)

    def values(self) -> dict[str, Any]:
        return self.configuration.load()

    def directory(self, key: str) -> Path:
        value = Path(self.values()[f"{self.prefix}_{key}_DIRECTORY"]).expanduser()
        return (value if value.is_absolute() else self.root / value).resolve()

    def proxy_settings(self) -> dict:
        values = self.values()
        path = Path(values[f"{self.prefix}_HOST_PROXY_FILE"]).expanduser()
        snapshot = path if path.is_absolute() else self.root / path
        value = values[f"{self.prefix}_HTTP_PROXY"]
        if self.proxy_resolver is not None:
            return self.proxy_resolver(value, snapshot)
        return client_proxy(value, snapshot)

    def environment(self) -> dict[str, str]:
        proxy = self.proxy_settings()
        env = subprocess_environment(proxy["proxy"], proxy["no_proxy"])
        home = self.directory("AUTH")
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        env["HOME"] = str(home)
        env.pop("CODEX_HOME", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if self.name == "codex":
            env["CODEX_HOME"] = str(home / ".codex")
        else:
            env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        Path(env["CODEX_HOME"] if self.name == "codex" else env["CLAUDE_CONFIG_DIR"]).mkdir(
            parents=True, exist_ok=True, mode=0o700)
        # The browser runs on the user's device; only the authorization link crosses the UI.
        env["BROWSER"] = "true"
        env["NO_COLOR"] = "1"
        return env

    def network_routes(self):
        settings = self.proxy_settings()
        return (DiagnosticNetworkRoute("客户端配置的 HTTP 路径", settings["proxy"], settings["no_proxy"]),)

    def executable(self) -> str:
        root = self.directory("CLIENT")
        pointer = root / "active.json"
        if pointer.exists():
            relative = json.loads(pointer.read_text())["executable"]
            candidate = (root / relative).resolve()
            if root not in candidate.parents:
                raise ValueError("Managed client path is outside its installation directory")
            return resolve_executable(str(candidate))
        return resolve_executable(self.values()[f"{self.prefix}_CLI_PATH"])

    def start(self) -> None:
        with self.lock:
            if self.monitor or self.closed:
                return
            self.monitor = threading.Thread(target=self._maintenance, daemon=True,
                                            name=f"{self.name}-account")
            self.monitor.start()

    def _maintenance(self) -> None:
        while not self.stop.wait(5):
            self.inspect()
            try:
                values = self.values()
                interval = max(60, values[f"{self.prefix}_UPDATE_CHECK_SECONDS"])
                if values[f"{self.prefix}_CHECK_UPDATES"] and time.time() - self.last_update_check >= interval:
                    self.check_update()
            except (ValueError, KeyError):
                pass  # inspect reports configuration errors; no fallback credentials are used.
            if self.stop.wait(25):
                return

    def _run(self, arguments: list[str], timeout: int = 15):
        return subprocess.run([self.executable(), *arguments], env=self.environment(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, check=False)

    def inspect(self) -> None:
        if not self.inspection_lock.acquire(blocking=False):
            return
        try:
            self._inspect()
        finally:
            self.inspection_lock.release()

    def _inspect(self) -> None:
        with self.lock:
            if self.process is not None or self.closed:
                return
        try:
            result = self._run(["login", "status"] if self.name == "codex" else ["auth", "status", "--json"])
            logged_in = result.returncode == 0
            if self.name == "claude" and logged_in:
                logged_in = json.loads(result.stdout).get("loggedIn") is True
            version = self._run(["--version"]).stdout
            installed = self._version(version)
            notice = self.directory("AUTH") / "reauthentication.json"
            rejected = notice.exists()
            with self.lock:
                if self.process is not None:
                    return
                self.state.update(state="login_required" if rejected or not logged_in else "authenticated",
                                  message="登录已失效，请重新登录" if rejected else
                                  ("已登录；凭据由客户端保存，远端有效性在请求时验证" if logged_in else "尚未登录，请点击登录"),
                                  installed_version=installed, checked_at=time.time())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            with self.lock:
                self.state.update(state="error", message="无法检查客户端，请检查命令路径、配置和代理", checked_at=time.time())

    @staticmethod
    def _version(value: str) -> str:
        match = re.search(r"\b\d+\.\d+\.\d+(?:-[\w.-]+)?\b", value)
        return match[0] if match else ""

    def record_auth_failure(self, output: str) -> None:
        # Only explicit authentication failures prompt re-login; rate limits/network failures do not.
        if re.search(r"(?i)(unauthorized|invalid[_ ](?:access[_ ])?token|token.{0,30}expired|"
                     r"refresh_token_reused|authentication[_ ](?:error|failed)|not logged in|please (?:run .?login|log in)|"
                     r"(?:status|http|error)\s*[:=]?\s*401\b)", output):
            atomic_write_text(self.directory("AUTH") / "reauthentication.json",
                              json.dumps({"detected_at": time.time()}))
            with self.lock:
                self.state.update(state="login_required", message="登录已失效，请重新登录")

    def snapshot(self) -> dict[str, Any]:
        self.start()
        with self.lock:
            value = dict(self.state)
            try:
                proxy = self.proxy_settings()
                value["proxy_message"] = proxy["display"] + " — " + proxy["source"]
            except ValueError as error:
                value["proxy_message"] = str(error)
            if self.relay:
                value.update(self.relay.status())
                value["wizard"] = True
            if value.get("login_mode") == "remote":
                value["wizard"] = True
            value["actions"] = [
                {"id": "login", "label": "登录 / 重新登录"},
                {"id": "cancel", "label": "取消登录"},
                {"id": "logout", "label": "退出账号", "confirm": "退出此客户端账号？"},
                {"id": "check", "label": "检查登录与版本"},
                {"id": "export_credentials", "label": "导出登录凭据",
                 "disabled": value["state"] != "authenticated",
                 "confirm": "导出的文件包含可直接使用的登录令牌。任何拿到它的人都能以此账号发起请求；请只保存在你信任的位置。"},
                {"id": "import_credentials", "label": "导入登录凭据",
                 "fields": [{"name": "bundle", "label": "凭据文件", "type": "file",
                             "description": "选择本机器人导出的同一客户端凭据文件；导入会覆盖当前登录状态。"}],
                 "confirm": "用文件中的凭据覆盖当前登录状态？"},
                {"id": "upgrade", "label": "升级客户端", "disabled": not value["update_available"] or value["update_state"] == "installing",
                 "confirm": "安装官方最新客户端？成功后用于后续请求，已有请求继续使用旧版本。"},
            ]
            return value

    def action(self, action: str, values: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise ValueError("Plugin has been unloaded")
        if action in {"login", "login_remote"}:
            self.login(origin=values.get("_public_origin", ""), remote=action == "login_remote")
        elif action == "login_code":
            with self.lock:
                if (self.name != "claude" or self.state.get("login_mode") != "remote"
                        or self.process is None or self.process.poll() is not None
                        or values.get("flow_id") != self.flow_id or self.state.get("code_submitted")):
                    raise ValueError("验证码不属于当前等待的登录流程，请重新开始")
                code = str(values.get("code", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_#.~+/=-]{1,8192}", code):
                    raise ValueError("请输入官方页面显示的完整验证码，不要输入密码或多行内容")
                self.process.stdin.write(code + "\n")
                self.process.stdin.flush()
                self.state.update(code_submitted=True, message="已提交验证码，等待官方客户端验证…")
        elif action == "callback_direct_ready":
            if not self.relay or self.process is None or values.get("flow_id") != self.flow_id:
                raise ValueError("登录流程已变更，请重新检测")
            self.relay.direct_ready(values.get("redirect_uri", ""), values.get("proof", ""))
        elif action == "helper_command":
            if not self.relay or self.process is None or values.get("flow_id") != self.flow_id:
                raise ValueError("请先在界面开始登录")
            return self.relay.script_command(values["_public_origin"], values.get("platform", ""), self.name)
        elif action == "cancel":
            self.cancel()
        elif action == "logout":
            self.cancel()
            result = self._run(["logout"] if self.name == "codex" else ["auth", "logout"])
            if result.returncode:
                raise ValueError("客户端退出失败，请检查客户端状态")
            (self.directory("AUTH") / "reauthentication.json").unlink(missing_ok=True)
            self.inspect()
        elif action == "check":
            self._background_check()
        elif action == "upgrade":
            self.upgrade()
        elif action == "export_credentials":
            return {**self.snapshot(), "credential_export": self.export_credentials()}
        elif action == "import_credentials":
            self.import_credentials(values.get("bundle", ""))
        else:
            raise ValueError("Unknown client action")
        return self.snapshot()

    def export_credentials(self) -> dict[str, Any]:
        """Package the files that restore this client's signed-in session.

        Logging in happens through the official client, in a browser, on the operator's device.
        That is fine once; repeating it for every fresh container is not, so the session is made
        portable. The bundle carries live tokens, which is why the surrounding action states that
        plainly rather than presenting it as an ordinary settings file.
        """
        home = self.directory("AUTH")
        files: dict[str, str] = {}
        for relative in CREDENTIAL_FILES.get(self.name, ()):
            path = home / relative
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) > CREDENTIAL_FILE_MAX_BYTES:
                raise ValueError(f"凭据文件过大，无法导出：{relative}")
            files[relative] = base64.b64encode(raw).decode("ascii")
        if not files:
            raise ValueError("当前没有可导出的登录凭据，请先在界面完成登录")
        return {
            "kind": CREDENTIAL_BUNDLE_KIND,
            "version": CREDENTIAL_BUNDLE_VERSION,
            "client": self.name,
            "exported_at": int(time.time()),
            "warning": "This file contains live login tokens for the named client.",
            "files": files,
        }

    def import_credentials(self, bundle: str) -> None:
        """Restore a previously exported session into this client's private home."""
        text = str(bundle or "").strip()
        if not text:
            raise ValueError("请选择要导入的凭据文件")
        if len(text) > 8 * CREDENTIAL_FILE_MAX_BYTES:
            raise ValueError("凭据文件过大")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"凭据文件不是有效 JSON：{error}") from error
        if not isinstance(payload, dict) or payload.get("kind") != CREDENTIAL_BUNDLE_KIND:
            raise ValueError("这不是本机器人导出的凭据文件")
        if int(payload.get("version", 0)) != CREDENTIAL_BUNDLE_VERSION:
            raise ValueError("凭据文件版本不受支持")
        if str(payload.get("client", "")) != self.name:
            raise ValueError(
                f"凭据属于 {payload.get('client', '未知')} 客户端，不能导入到 {self.name}"
            )
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("凭据文件不包含任何内容")
        allowed = set(CREDENTIAL_FILES.get(self.name, ()))
        decoded: dict[str, bytes] = {}
        for relative, encoded in files.items():
            if relative not in allowed:
                raise ValueError(f"凭据文件包含不接受的条目：{relative}")
            try:
                raw = base64.b64decode(str(encoded), validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError(f"凭据内容无法解码：{relative}") from error
            if len(raw) > CREDENTIAL_FILE_MAX_BYTES:
                raise ValueError(f"凭据文件过大：{relative}")
            decoded[relative] = raw
        self.cancel()
        home = self.directory("AUTH")
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        for relative, raw in decoded.items():
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            path.write_bytes(raw)
            path.chmod(0o600)
        (home / "reauthentication.json").unlink(missing_ok=True)
        self.state.update(message="已导入登录凭据，正在验证…")
        self.inspect()

    def login(self, *, origin: str = "", remote: bool = False) -> None:
        with self.lock:
            if self.process is not None:
                raise ValueError("登录流程已启动，请先完成或取消")
            timeout = max(30, int(self.values()[f"{self.prefix}_LOGIN_TIMEOUT_SECONDS"]))
            arguments = ["login"] if self.name == "codex" else ["auth", "login", "--claudeai"]
            if remote and self.name == "codex":
                arguments.append("--device-auth")
            command = [self.executable(), *arguments]
            if self.name == "codex":
                command[1:1] = ["-c", 'cli_auth_credentials_store="file"']
            env = self.environment()
            scratch = tempfile.TemporaryDirectory(prefix=".login-", dir=env["HOME"])
            root = Path(scratch.name)
            launcher = root / "xdg-open"
            atomic_write_text(launcher, "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                              shlex.quote(str(Path(__file__).with_name("_browser_capture.py"))) + ' "$@"\n')
            if remote:
                # Let the official CLI select its supported non-browser code flow.
                atomic_write_text(launcher, "#!/bin/sh\nexit 1\n")
            launcher.chmod(0o700)
            (root / "sensible-browser").symlink_to(launcher)
            env.update(BROWSER=str(launcher), PATH=str(root) + os.pathsep + env["PATH"],
                       PREDICTION_CLIENT_BROWSER_REQUEST=str(root / "browser.json"))
            bind_host = self.values()[f"{self.prefix}_CALLBACK_BIND_HOST"]
            if bind_host == "AUTO":
                bind_host = socket.gethostbyname(socket.gethostname()) if Path("/.dockerenv").exists() else ""
                if bind_host and ip_address(bind_host).is_loopback:
                    bind_host = ""
            if self.relay:
                self.relay.close()
            self.relay = None if remote else BrowserLoginRelay(timeout, bind_host=bind_host, origin=origin,
                                           completion_paths=("/success",) if self.name == "codex" else ())
            self.state.update(login_mode="remote" if remote else "local", code_submitted=False)
            self.process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
            self.flow_id = uuid.uuid4().hex
            self.state.update(state="authorizing", message="正在获取官方授权链接…", flow_id=self.flow_id,
                              authorization_url="", device_code="", expires_at=time.time() + timeout)
            self.browser_thread = None
            if not remote:
                self.browser_thread = threading.Thread(target=self._capture_browser, args=(self.process, root / "browser.json"), daemon=True)
                self.browser_thread.start()
            self.login_thread = threading.Thread(target=self._read_login, args=(self.process, timeout, scratch), daemon=True)
            self.login_thread.start()

    def helper_message(self, token: str, envelope: dict) -> dict:
        relay = self.relay
        if not relay or self.closed:
            raise ValueError("No active login relay")
        if set(envelope) == {"script_platform"}:
            return relay.script(token, envelope["script_platform"])
        return relay.handle(token, envelope)

    def _capture_browser(self, process, path):
        while self.process is process and process.poll() is None:
            try:
                if path.exists():
                    self._login_instructions(json.loads(path.read_text())["url"])
                    return
            except (OSError, ValueError, KeyError):
                pass  # Capture file may still be in flight; never use a partial authorization URL.
            if self.stop.wait(.1):
                return

    def _read_login(self, process: subprocess.Popen, timeout: int, scratch=None) -> None:
        timed_out = threading.Event()
        def expire():
            timed_out.set()
            if process.poll() is None:
                self._kill_install(process)
        timer = threading.Timer(timeout, expire)
        timer.start()
        transcript = ""
        try:
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                transcript = (transcript + char)[-32768:]
                if char.isspace():
                    self._login_instructions(transcript)
            code = process.wait()
            self._login_instructions(transcript)
            with self.lock:
                if self.process is not process:
                    return
                self.process = None
                self.state.update(authorization_url="", device_code="", flow_id="")
                if code != 0:
                    self.state.update(state="login_required", message="登录超时，请重试" if timed_out.is_set() else "登录未完成，请检查代理后重试")
                    return
            notice = self.directory("AUTH") / "reauthentication.json"
            notice.unlink(missing_ok=True)
            self.inspect()
        except (OSError, ValueError):
            with self.lock:
                if self.process is process:
                    self.process = None
                self.state.update(state="error", message="无法完成登录，请检查客户端与凭据目录权限",
                                  authorization_url="", device_code="", flow_id="")
        finally:
            timer.cancel()
            process.stdout.close()
            process.stdin.close()
            if self.relay:
                self.relay.close()
            if self.browser_thread:
                self.browser_thread.join(timeout=2)
            if scratch:
                scratch.cleanup()

    def _login_instructions(self, output: str) -> None:
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        hosts = {"auth.openai.com", "auth0.openai.com", "chatgpt.com"} if self.name == "codex" else {"claude.ai", "claude.com", "platform.claude.com", "console.anthropic.com"}
        for url in re.findall(r"https://[^\s<>\"\x1b]+", text):
            parsed = urlsplit(url)
            if parsed.hostname in hosts and not parsed.username and not parsed.password:
                with self.lock:
                    if self.state.get("login_mode") == "remote" and self.process is not None:
                        if self.name != "codex" or parsed.path.rstrip("/") == "/codex/device":
                            self.state.update(authorization_url=url, message="在官方页面完成授权，然后按向导确认。")
                    elif self.relay and self.relay.configure(url):
                        self.state.update(message="正在等待浏览器检测回调端口；可达时无需助手")
        if self.name == "codex" and self.state.get("login_mode") == "remote":
            code = re.search(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b", text)
            if code:
                with self.lock:
                    if self.process is not None:
                        self.state["device_code"] = code.group()

    def cancel(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            self.state.update(state="login_required", message="登录已取消", authorization_url="", device_code="", flow_id="")
        if process and process.poll() is None:
            self._kill_install(process)
        if self.login_thread and self.login_thread is not threading.current_thread():
            self.login_thread.join(timeout=4)
        if self.relay:
            self.relay.close()
            self.relay = None

    def _background_check(self) -> None:
        with self.lock:
            if self.update_thread and self.update_thread.is_alive():
                return
            def check():
                self.inspect()
                self.check_update()
            self.update_thread = threading.Thread(target=check, daemon=True)
            self.update_thread.start()

    def check_update(self) -> None:
        self.last_update_check = time.time()
        try:
            settings = self.proxy_settings()
            proxy = settings["proxy"]
            if urllib.request.proxy_bypass_environment("registry.npmjs.org", {"no": settings["no_proxy"]}):
                proxy = ""
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            with opener.open(f"https://registry.npmjs.org/{self.package}/latest", timeout=10) as response:
                version = json.loads(response.read(1024 * 1024))["version"]
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("Unexpected stable package version")
            with self.lock:
                installed = self.state["installed_version"]
                newer = not installed or tuple(map(int, version.split('.'))) > tuple(map(int, installed.split('-')[0].split('.')))
                self.state.update(latest_version=version, update_available=newer,
                                  update_checked_at=time.time(), update_message="发现新版，可点击升级" if newer else "已是最新版本")
        except (OSError, ValueError, KeyError, TypeError):
            with self.lock:
                self.state.update(update_message="版本检查失败，请检查代理或稍后重试", update_checked_at=time.time())

    def upgrade(self) -> None:
        with self.lock:
            if self.update_thread and self.update_thread.is_alive():
                raise ValueError("版本检查或升级正在进行")
            version = self.state["latest_version"]
            if not self.state["update_available"] or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("请先检查新版")
            self.state.update(update_state="installing", update_message="正在安装并验证新版，旧版仍可用")
            self.update_thread = threading.Thread(target=self._install, args=(version,), daemon=True)
            self.update_thread.start()

    def _install(self, version: str) -> None:
        process = None
        try:
            root = self.directory("CLIENT")
            target = root / f"{version}-{uuid.uuid4().hex[:12]}"
            target.mkdir(parents=True, mode=0o700)
            env = self.environment()
            npm = shutil.which("npm")
            if not npm:
                raise ValueError("npm is not installed")
            # Exact official package/version; no user-provided package or shell command is accepted.
            with self.lock:
                if self.closed:
                    return
                process = subprocess.Popen([npm, "install", "--prefix", str(target), "--registry=https://registry.npmjs.org",
                                            "--no-audit", "--no-fund", f"{self.package}@{version}"],
                                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True)
                self.install_process = process
            process.wait(timeout=300)
            if process.returncode:
                raise ValueError("Package install failed")
            executable = target / "node_modules" / ".bin" / self.name
            result = subprocess.run([str(executable), "--version"], env=env, capture_output=True,
                                    text=True, timeout=20, check=False)
            if result.returncode or self._version(result.stdout) != version:
                raise ValueError("Installed version verification failed")
            if self.stop.is_set():
                return
            atomic_write_text(root / "active.json", json.dumps({"executable": str(executable.relative_to(root))}))
            with self.lock:
                self.state.update(installed_version=version, update_state="idle", update_available=False,
                                  update_message="升级成功，后续请求使用新版")
        except (OSError, ValueError, subprocess.SubprocessError):
            with self.lock:
                self.state.update(update_state="failed", update_message="升级失败，旧版本保留；请检查代理、磁盘空间和 npm 后重试")
        finally:
            if process and process.poll() is None:
                self._kill_install(process)
            with self.lock:
                self.install_process = None

    @staticmethod
    def _kill_install(process: subprocess.Popen) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def teardown(self) -> None:
        self.closed = True
        self.stop.set()
        self.cancel()
        with self.lock:
            process = self.install_process
        if process and process.poll() is None:
            self._kill_install(process)
        if self.update_thread:
            self.update_thread.join(timeout=16)
        if self.monitor:
            self.monitor.join(timeout=31)
