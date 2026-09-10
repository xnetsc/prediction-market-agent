"""Read client model metadata without starting a prompt or a trading decision."""
from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time


EFFORT_LABELS = {"none": "不额外推理", "minimal": "最低", "low": "低", "medium": "中",
                 "high": "高", "xhigh": "极高", "max": "最高", "ultra": "超高"}


class ClientModelCatalog:
    def __init__(self, client, control):
        self.client, self.control = client, control
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0

    def models(self):
        with self.lock:
            if self.cached is not None and time.monotonic()-self.cached_at < 60:
                return self.cached
            command = [self.control.executable()]
            if self.client == "codex":
                command += ["app-server", "--listen", "stdio://"]
            else:
                command += ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                            "--verbose", "--no-session-persistence", "--strict-mcp-config",
                            "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}']
            try:
                with tempfile.TemporaryDirectory(prefix="client-model-list-") as directory:
                    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=directory,
                        env=self.control.environment())
                    try:
                        models = self._read(process)
                    finally:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=3)
                        process.stdin.close(); process.stdout.close()
            except (OSError, TimeoutError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                raise ValueError(f"{self.client} 模型列表读取失败（{type(error).__name__}）；请检查客户端安装、登录和代理后重试") from None
            if not models:
                raise ValueError(f"{self.client} 没有返回可选模型，请检查账号权限或升级客户端")
            self.cached, self.cached_at = models, time.monotonic()
            return models

    def _read(self, process):
        messages = queue.Queue(maxsize=128)
        stopped = threading.Event()
        def reader():
            try:
                while not stopped.is_set():
                    line = process.stdout.readline(2*1024*1024)
                    if not line:
                        messages.put_nowait(None); return
                    if len(line) >= 2*1024*1024:
                        messages.put_nowait(None); return
                    messages.put_nowait(line)
            except (OSError, ValueError, queue.Full):
                return
        thread = threading.Thread(target=reader, daemon=True); thread.start()
        deadline = time.monotonic()+25
        def send(value):
            process.stdin.write(json.dumps(value)+"\n"); process.stdin.flush()
        def receive():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            try:line=messages.get(timeout=remaining)
            except queue.Empty:raise TimeoutError() from None
            if line is None:raise ValueError("Client exited before responding")
            return json.loads(line)
        try:
            if self.client == "claude":
                send({"type":"control_request", "request_id":"model-catalog", "request":{"subtype":"initialize"}})
                while True:
                    message=receive()
                    response=message.get("response",{})
                    if message.get("type")=="control_response" and response.get("request_id")=="model-catalog":
                        if response.get("subtype")!="success":raise ValueError("Initialization failed")
                        return normalize_models(self.client,response.get("response",{}).get("models",[]))
            send({"id":1,"method":"initialize","params":{"clientInfo":{"name":"prediction_market_agent","version":"1.0"}}})
            while True:
                message=receive()
                if message.get("id")==1:
                    if "error" in message:raise ValueError("Initialization failed")
                    break
            send({"method":"initialized"})
            models=[];cursor=None;seen=set()
            for request_id in range(2,22):
                send({"id":request_id,"method":"model/list","params":{"limit":100,"includeHidden":False,"cursor":cursor}})
                while True:
                    message=receive()
                    if message.get("id")==request_id:break
                if "error" in message:raise ValueError("Model list failed")
                result=message["result"];models.extend(result["data"])
                cursor=result.get("nextCursor")
                if not cursor:return normalize_models(self.client,models)
                if cursor in seen:raise ValueError("Repeated model cursor")
                seen.add(cursor)
            raise ValueError("Too many model pages")
        finally:
            stopped.set()

    def choices(self, field, values):
        models=self.models()
        if field.endswith("_MODEL"):
            return [{"value":"","label":"使用客户端默认模型"}]+[
                {"value":m["value"],"label":m["label"]} for m in models]
        selected=values.get(self.client.upper()+"_MODEL","")
        model=next((m for m in models if m["value"]==selected),None)
        if not selected:model=next((m for m in models if m["default"]),None)
        efforts=model["efforts"] if model else []
        return [{"value":"","label":"使用客户端默认强度"}]+[
            {"value":value,"label":EFFORT_LABELS.get(value,value)+"（"+value+"）"} for value in efforts]


def normalize_models(client, values):
    if not isinstance(values,list):raise ValueError("Invalid model list")
    result=[]
    for item in values:
        if client=="codex":
            value=item.get("model") or item.get("id")
            efforts=[e["reasoningEffort"] for e in item.get("supportedReasoningEfforts",[])]
            default=bool(item.get("isDefault"))
        else:
            value=item.get("value");efforts=item.get("supportedEffortLevels",[]) if item.get("supportsEffort") else []
            default=value=="default"
        if not isinstance(value,str) or not value or not isinstance(efforts,list) or not all(isinstance(e,str) for e in efforts):
            raise ValueError("Invalid model metadata")
        result.append({"value":value,"label":str(item.get("displayName") or value)+" · "+value,
                       "efforts":list(dict.fromkeys(efforts)),"default":default})
    return result
