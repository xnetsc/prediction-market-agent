from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult
from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginReadiness,
    PluginSpec,
)
from prediction_market_agent.plugins.providers._model_catalog import ClientModelCatalog
from prediction_market_agent.plugins.providers._shared import resolve_executable, subprocess_environment
from prediction_market_agent.plugins.providers._client_control import ClientControl, client_fields, client_configuration_loader


class CodexCliBackend:
    name = "codex"

    def __init__(self, executable: str, model: str, proxy: str, timeout: int, control: ClientControl | None = None, effort: str = ""):
        self.executable = resolve_executable(executable)
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.control = control
        self.effort = effort

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        with tempfile.TemporaryDirectory(prefix="prediction-codex-") as directory:
            root = Path(directory)
            schema_path = root / f"{schema_name}.schema.json"
            output_path = root / f"{schema_name}.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self.control.executable() if self.control else self.executable, "exec", "--ephemeral", "--skip-git-repo-check",
                "--ignore-rules", "--sandbox", "read-only", "--color", "never",
                "--output-schema", str(schema_path), "--output-last-message",
                str(output_path), "-C", str(root),
            ]
            if self.model:
                command.extend(["--model", self.model])
            if self.effort:
                command.extend(["-c", "model_reasoning_effort="+json.dumps(self.effort)])
            command.append("-")
            try:
                completed = subprocess.run(
                    command, input=prompt, text=True, capture_output=True,
                    timeout=self.timeout,
                    env=self.control.environment() if self.control else subprocess_environment(self.proxy),
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
                if self.control:
                    self.control.record_auth_failure(completed.stderr + completed.stdout)
                raise DecisionProviderError(f"Codex failed: {completed.stderr[-1000:]}", raw)
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DecisionProviderError("Codex returned invalid JSON", raw) from error
            if not isinstance(value, dict):
                raise DecisionProviderError("Codex returned non-object JSON", raw)
            return StructuredResult(value, raw)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "codex.json")
    load = client_configuration_loader(load, "CODEX")
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("CODEX_CLI_PATH", "Codex 命令", "string", "镜像或本机 Codex 命令；界面升级后的安装优先使用。", required=True, default="codex"),
            PluginConfigField("CODEX_MODEL", "Codex 模型", "string", "从当前客户端返回的模型列表选择；留空使用客户端默认。列表不保证账号额度或每次调用成功。", default="", selection_only=True),
            PluginConfigField("CODEX_EFFORT", "推理强度", "string", "选择模型后列出它支持的强度；越高通常越慢、消耗越多。无选项表示客户端未声明支持。留空使用客户端默认。", default="", selection_only=True, choices_depend_on=("CODEX_MODEL",)),
            PluginConfigField("CODEX_HTTP_PROXY", "代理使用方式", "string", "默认 INHERIT，使用程序设置里的统一代理。也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM（仅原生 macOS）或完整 http(s) URL；登录、调用与升级共用。", required=True, default="INHERIT"),
            PluginConfigField("CODEX_TIMEOUT_SECONDS", "超时秒数", "integer", "每次 Codex 结构化调用的超时时间。", required=True, default=180),
        ) + client_fields("CODEX"), load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
        choice_fields=("CODEX_MODEL", "CODEX_EFFORT"), context_choices_callback=lambda field, values: model_catalog.choices(field,values),
    )
    control = ClientControl(
        "codex",
        "@openai/codex",
        configuration,
        context.working_directory,
        lambda value, snapshot: context.proxy_settings(
            value, field_name="CODEX_HTTP_PROXY", snapshot_file=snapshot
        ),
    )
    model_catalog = ClientModelCatalog("codex", control)

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["CODEX_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CODEX_TIMEOUT_SECONDS must be at least 10")
        return CodexCliBackend(
            control.executable(),
            values["CODEX_MODEL"].strip(),
            control.proxy_settings()["proxy"],
            timeout, control, values["CODEX_EFFORT"].strip(),
        )

    def readiness() -> PluginReadiness:
        try:
            factory(None)
            control.inspect()
            status = control.snapshot()
        except (OSError, ValueError, DecisionProviderError) as error:
            return PluginReadiness(False, (str(error),))
        if status.get("state") != "authenticated":
            return PluginReadiness(
                False, (str(status.get("message") or "Codex 客户端尚未登录"),)
            )
        return PluginReadiness(True)

    control.start()
    return PluginSpec("decision_provider", "codex", "通过 Codex 客户端进行结构化 Agent 决策，支持独立登录及升级。", str(context.module_path), factory, configuration, control.teardown, readiness_callback=readiness, controls=control.controls, network_routes_callback=control.network_routes)
