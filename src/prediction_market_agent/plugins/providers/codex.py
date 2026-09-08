from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult
from prediction_market_agent.sdk.config_io import json_file_callbacks, resolve_plugin_proxy
from prediction_market_agent.sdk.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.plugins.providers._shared import resolve_executable, subprocess_environment


class CodexCliBackend:
    name = "codex"

    def __init__(self, executable: str, model: str, proxy: str, timeout: int):
        self.executable = resolve_executable(executable)
        self.model = model
        self.proxy = proxy
        self.timeout = timeout

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        with tempfile.TemporaryDirectory(prefix="prediction-codex-") as directory:
            root = Path(directory)
            schema_path = root / f"{schema_name}.schema.json"
            output_path = root / f"{schema_name}.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self.executable, "exec", "--ephemeral", "--skip-git-repo-check",
                "--ignore-rules", "--sandbox", "read-only", "--color", "never",
                "--output-schema", str(schema_path), "--output-last-message",
                str(output_path), "-C", str(root),
            ]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            try:
                completed = subprocess.run(
                    command, input=prompt, text=True, capture_output=True,
                    timeout=self.timeout,
                    env=subprocess_environment(self.proxy),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raw = json.dumps({"timeout": True, "stdout": error.stdout or "", "stderr": error.stderr or ""}, ensure_ascii=False)
                raise DecisionProviderError(f"Codex timed out after {self.timeout}s", raw) from error
            raw = json.dumps({
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "final": output_path.read_text(encoding="utf-8") if output_path.exists() else "",
            }, ensure_ascii=False)
            if completed.returncode != 0 or not output_path.exists():
                raise DecisionProviderError(f"Codex failed: {completed.stderr[-1000:]}", raw)
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DecisionProviderError("Codex returned invalid JSON", raw) from error
            if not isinstance(value, dict):
                raise DecisionProviderError("Codex returned non-object JSON", raw)
            return StructuredResult(value, raw)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "codex.json")
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("CODEX_CLI_PATH", "Codex 命令", "string", "本机 Codex CLI 的命令名或可执行文件绝对路径。", required=True),
            PluginConfigField("CODEX_MODEL", "Codex 模型", "string", "Codex 客户端使用的模型覆盖；空字符串表示使用客户端默认模型。"),
            PluginConfigField("CODEX_HTTP_PROXY", "HTTP 代理", "string", "Codex 子进程独立使用的代理；DIRECT、SYSTEM 或 http(s) URL。", required=True),
            PluginConfigField("CODEX_TIMEOUT_SECONDS", "超时秒数", "integer", "每次 Codex 结构化调用的超时时间。", required=True),
        ), load_callback=load, save_callback=save, storage=storage,
    )

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["CODEX_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CODEX_TIMEOUT_SECONDS must be at least 10")
        return CodexCliBackend(
            values["CODEX_CLI_PATH"].strip(),
            values["CODEX_MODEL"].strip(),
            resolve_plugin_proxy(values["CODEX_HTTP_PROXY"], field_name="CODEX_HTTP_PROXY"),
            timeout,
        )

    return PluginSpec("decision_provider", "codex", "通过本机 Codex 客户端进行结构化 Agent 决策。", str(context.module_path), factory, configuration, lambda: None)
