"""One-flow capability relay for official CLI loopback OAuth callbacks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ._callback_gateway import CallbackGateway
from ._helper_scripts import command_for, render_script


def seal(secret: str, value: dict, direction: str, *, algorithm: str = "AES-GCM") -> dict:
    if algorithm == "AES-CBC-HMAC-SHA256":
        keys = hashlib.sha512(b"prediction-login-v1\0" + secret.encode()).digest()
        nonce = secrets.token_bytes(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(json.dumps(value).encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(keys[:32]), modes.CBC(nonce)).encryptor()
        cipher = encryptor.update(padded) + encryptor.finalize()
        mac = hmac.digest(keys[32:], direction.encode() + nonce + cipher, "sha256")
        return {"algorithm": algorithm, "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(cipher).decode(), "mac": base64.b64encode(mac).decode()}
    if algorithm != "AES-GCM":
        raise ValueError("Unsupported helper encryption")
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(hashlib.sha256(secret.encode()).digest()).encrypt(
        nonce, json.dumps(value).encode(), direction.encode())
    return {"nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(cipher).decode()}


def unseal(secret: str, envelope: dict, direction: str) -> dict:
    algorithm = envelope.get("algorithm", "AES-GCM")
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    cipher = base64.b64decode(envelope["ciphertext"], validate=True)
    if algorithm == "AES-CBC-HMAC-SHA256":
        keys = hashlib.sha512(b"prediction-login-v1\0" + secret.encode()).digest()
        expected = hmac.digest(keys[32:], direction.encode() + nonce + cipher, "sha256")
        if len(nonce) != 16 or not hmac.compare_digest(expected, base64.b64decode(envelope["mac"], validate=True)):
            raise ValueError("Invalid helper authentication tag")
        decryptor = Cipher(algorithms.AES(keys[:32]), modes.CBC(nonce)).decryptor()
        padded = decryptor.update(cipher) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
    elif algorithm == "AES-GCM":
        raw = AESGCM(hashlib.sha256(secret.encode()).digest()).decrypt(nonce, cipher, direction.encode())
    else:
        raise ValueError("Unsupported helper encryption")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid relay message")
    return value


class CompletionRedirect(urllib.request.HTTPRedirectHandler):
    """Follow only provider-declared completion paths on the original CLI listener."""

    max_redirections = 3
    max_repeats = 1

    def __init__(self, redirect_uri: str, paths: tuple[str, ...]):
        self.callback = urlsplit(redirect_uri)
        self.paths = paths

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if ((target.scheme, target.hostname, target.port) !=
                (self.callback.scheme, self.callback.hostname, self.callback.port)
                or target.username or target.password or target.fragment or target.path not in self.paths):
            raise ValueError("客户端返回了未支持的登录完成跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BrowserLoginRelay:
    def __init__(self, timeout: int, *, bind_host: str = "", origin: str = "",
                 completion_paths: tuple[str, ...] = ()):
        self.secret = secrets.token_urlsafe(32)
        self.expires_at = time.time() + timeout
        self.lock = threading.RLock()
        self.seen: set[str] = set()
        self.authorization_url = ""
        self.redirect_uri = ""
        self.oauth_state = ""
        self.ready = False
        self.callback_mode = ""
        self.consumed = False
        self.closed = False
        self.message = "等待在终端运行本次登录命令"
        self.script_tickets: dict[str, tuple[str, str]] = {}
        self.bind_host, self.origin = bind_host, origin
        self.completion_paths = completion_paths
        self.gateway: CallbackGateway | None = None
        self.gateway_attempted = False
        self.gateway_error = ""

    def configure(self, url: str) -> bool:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        redirect = query.get("redirect_uri", [""])[0]
        callback = urlsplit(redirect)
        if (callback.scheme != "http" or callback.hostname not in {"localhost", "127.0.0.1"}
                or callback.path not in {"/auth/callback", "/callback"}
                or not callback.port or callback.username or callback.password
                or callback.query or callback.fragment or not query.get("state")):
            return False
        with self.lock:
            if self.authorization_url and self.authorization_url != url:
                return False
            self.authorization_url, self.redirect_uri = url, redirect
            self.oauth_state = query["state"][0]
            if self.bind_host and self.origin and not self.gateway_attempted:
                self.gateway_attempted = True
                try:
                    self.gateway = CallbackGateway(self, self.bind_host, self.origin)
                except OSError:
                    self.gateway_error = "容器回调验证入口无法监听，不能确认端口映射；可使用助手"
            return True

    def status(self) -> dict:
        with self.lock:
            return {"helper_ready": self.ready, "helper_message": self.message,
                    "authorization_url": self.authorization_url if self.ready else "",
                    "redirect_uri": self.redirect_uri if not self.closed else "",
                    "callback_mode": self.callback_mode,
                    "callback_pending": not self.closed and not self.consumed and time.time() < self.expires_at,
                    "callback_probe": self.gateway.status() if self.gateway and not self.gateway.closed and not self.closed else None,
                    "callback_probe_error": self.gateway_error,
                    "expires_at": self.expires_at}

    def direct_ready(self, redirect_uri: str, proof: str) -> None:
        """Require a probe observed at this flow's own mapped listener."""
        with self.lock:
            if self.closed or time.time() >= self.expires_at or self.consumed:
                raise ValueError("Login flow is no longer active")
            if not self.redirect_uri or redirect_uri != self.redirect_uri:
                raise ValueError("Callback address mismatch")
            if (not self.gateway or self.gateway.closed or not self.gateway.observed
                    or not isinstance(proof, str) or not hmac.compare_digest(proof, self.gateway.proof)):
                raise ValueError("回调端口尚未验证为当前容器的本次登录入口")
            if self.callback_mode == "helper":
                raise ValueError("助手已接管回调；请先重新开始登录再检测直连")
            self.ready = True
            self.callback_mode = "direct"
            self.message = "浏览器已验证回调端口映射到本次登录入口，无需助手"

    def forward_callback(self, query: str) -> None:
        with self.lock:
            if not self.ready or self.consumed or self.closed or time.time() >= self.expires_at:
                raise ValueError("Callback is not active")
            if not isinstance(query, str) or len(query) > 16384 or any(c in query for c in "\r\n"):
                raise ValueError("Invalid callback")
            values = parse_qs(query)
            if values.get("state") != [self.oauth_state] or not (values.get("code") or values.get("error")):
                raise ValueError("OAuth state mismatch")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), CompletionRedirect(self.redirect_uri, self.completion_paths))
            try:
                with opener.open(self.redirect_uri + "?" + query, timeout=30) as response:
                    status = response.status
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
            if not 200 <= status < 300:
                raise ValueError("客户端拒绝了授权回调，请在界面重试")
            self.consumed = True
            self.message = "授权回调已送达，正在等待客户端确认登录"

    def script_command(self, origin: str, platform: str, provider: str) -> dict:
        if platform not in {"bash", "powershell"}:
            raise ValueError("Unsupported helper shell")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or origin != self.origin):
            raise ValueError("远程助手需要 HTTPS 管理地址")
        config = {"endpoint": origin.rstrip('/') + "/api/plugin-helper/decision_provider/" + provider,
                  "secret": self.secret, "expires_at": self.expires_at}
        script = render_script(platform, config)
        with self.lock:
            if self.closed or self.consumed or self.ready or time.time() >= self.expires_at:
                raise ValueError("本次登录已结束或回调已就绪，请重新开始")
            ticket = secrets.token_urlsafe(32)
            self.script_tickets.clear()
            self.script_tickets[ticket] = (platform, script)
        url = config["endpoint"] + "/script/" + platform
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        return {"command": command_for(url, ticket, platform, digest), "expires_at": self.expires_at}

    def script(self, token: str, platform: str) -> dict:
        with self.lock:
            if self.closed or self.ready or self.consumed or time.time() >= self.expires_at:
                raise ValueError("Login script expired")
            entry = self.script_tickets.get(token)
            if not entry or entry[0] != platform:
                raise ValueError("Invalid one-use script capability")
            script = entry[1]
            del self.script_tickets[token]
            return {"script": script}

    def handle(self, token: str, envelope: dict) -> dict:
        with self.lock:
            if self.closed or time.time() >= self.expires_at or not hmac.compare_digest(token, self.secret):
                raise ValueError("登录助手配对失效，请在界面重新开始")
            if envelope.get("nonce") in self.seen:
                raise ValueError("Repeated helper request")
            payload = unseal(self.secret, envelope, "helper-request")
            self.seen.add(envelope["nonce"])
            if len(self.seen) > 2048:
                raise ValueError("Helper request limit exceeded")
            action = payload.get("action")
            if action == "poll":
                result = {"redirect_uri": self.redirect_uri, "completed": self.consumed}
            elif action == "ready":
                if payload.get("redirect_uri") != self.redirect_uri or not self.redirect_uri:
                    raise ValueError("Callback address mismatch")
                self.ready = True
                self.callback_mode = "helper"
                if self.gateway:
                    self.gateway.close()
                self.message = "本地助手已连接，回调端口已就绪；现在可打开官方登录页"
                result = {"ok": True}
            elif action == "error":
                self.ready = False
                self.message = "助手无法监听本地回调端口；请关闭占用该端口的登录流程后重试"
                result = {"ok": True}
            elif action == "callback":
                self.forward_callback(payload.get("query", ""))
                result = {"ok": True}
            else:
                raise ValueError("Unknown helper operation")
            return seal(self.secret, result, "helper-response", algorithm=envelope.get("algorithm", "AES-GCM"))

    def close(self):
        with self.lock:
            self.closed = True
            self.ready = False
            self.script_tickets.clear()
            gateway, self.gateway = self.gateway, None
        if gateway:
            gateway.close()
