from __future__ import annotations

import json
import subprocess
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult
from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginReadiness, PluginSpec
from prediction_market_agent.plugins.providers._model_catalog import ClientModelCatalog
from prediction_market_agent.plugins.providers._shared import resolve_executable, subprocess_environment
from prediction_market_agent.plugins.providers._client_control import ClientControl, client_fields, client_configuration_loader


class ClaudeCliBackend:
    name = "claude"

    def __init__(self, executable: str, model: str, proxy: str, timeout: int, control: ClientControl | None = None, effort: str = ""):
        self.executable = resolve_executable(executable)
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.control = control
        self.effort = effort

    def entitlement(self) -> bool | None:
        """Whether this account can serve a request, asked without spending anything.

        The client knows its own quota and its own session state, so both questions are put to it
        rather than answered by sending a request and seeing what happens. Returns None when the
        client cannot say, which leaves the caller free to find out the expensive way.
        """
        if not self.control:
            return None
        usage = self.control.usage()
        if usage.get("available") is not None:
            return bool(usage["available"])
        return self.control.authenticated()

    def liveness(self) -> bool | None:
        """Free signal the client provides about its own session; None when unavailable."""
        return self.control.authenticated() if self.control else None

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        del schema_name
        command = [self.control.executable() if self.control else self.executable, "-p", "--no-session-persistence", "--permission-mode", "plan", "--max-turns", "1", "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":"))]
        if self.model:
            command.extend(["--model", self.model])
        if self.effort:
            command.extend(["--effort", self.effort])
        try:
            completed = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=self.timeout, env=self.control.environment() if self.control else subprocess_environment(self.proxy), check=False)
        except subprocess.TimeoutExpired as error:
            raw = json.dumps({"timeout": True, "stdout": error.stdout or "", "stderr": error.stderr or ""}, ensure_ascii=False)
            raise DecisionProviderError(f"Claude timed out after {self.timeout}s", raw) from error
        raw = json.dumps({"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
        if completed.returncode != 0:
            if self.control:
                self.control.record_auth_failure(completed.stderr + completed.stdout)
            raise DecisionProviderError(f"Claude failed: {completed.stderr[-1000:]}", raw)
        try:
            envelope = json.loads(completed.stdout)
            if envelope.get("is_error"):
                if self.control:
                    self.control.record_auth_failure(str(envelope.get("result", "")))
                raise DecisionProviderError("Claude returned an error result", raw)
            value = envelope["structured_output"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise DecisionProviderError("Claude returned invalid structured output", raw) from error
        if not isinstance(value, dict):
            raise DecisionProviderError("Claude returned non-object structured output", raw)
        return StructuredResult(value, raw)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "claude.json")
    load = client_configuration_loader(load, "CLAUDE")
    configuration = PluginConfiguration(fields=(
        PluginConfigField("CLAUDE_CLI_PATH", "Claude 命令", "string", "镜像或本机 Claude 命令；界面升级后的安装优先使用。", required=True, default="claude"),
        PluginConfigField("CLAUDE_MODEL", "Claude 模型", "string", "从当前客户端返回的模型列表选择；留空使用客户端默认。列表不保证账号额度或每次调用成功。", default="", selection_only=True),
        PluginConfigField("CLAUDE_EFFORT", "推理强度", "string", "选择模型后列出它支持的强度；越高通常越慢、消耗越多。无选项表示客户端未声明支持。留空使用客户端默认。", default="", selection_only=True, choices_depend_on=("CLAUDE_MODEL",)),
        PluginConfigField("CLAUDE_HTTP_PROXY", "代理使用方式", "string", "默认 INHERIT，使用程序设置里的统一代理。也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM（仅原生 macOS）或完整 http(s) URL；登录、调用与升级共用。", required=True, default="INHERIT"),
        PluginConfigField("CLAUDE_TIMEOUT_SECONDS", "超时秒数", "integer", "每次 Claude 结构化调用的超时时间。", required=True, default=180),
    ) + client_fields("CLAUDE"), load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
        choice_fields=("CLAUDE_MODEL", "CLAUDE_EFFORT"), context_choices_callback=lambda field, values: model_catalog.choices(field,values))
    control = ClientControl(
        "claude",
        "@anthropic-ai/claude-code",
        configuration,
        context.working_directory,
        lambda value, snapshot: context.proxy_settings(
            value, field_name="CLAUDE_HTTP_PROXY", snapshot_file=snapshot
        ),
    )
    model_catalog = ClientModelCatalog("claude", control)

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["CLAUDE_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CLAUDE_TIMEOUT_SECONDS must be at least 10")
        return ClaudeCliBackend(control.executable(), values["CLAUDE_MODEL"].strip(), control.proxy_settings()["proxy"], timeout, control, values["CLAUDE_EFFORT"].strip())

    def readiness() -> PluginReadiness:
        try:
            factory(None)
            control.inspect()
            status = control.snapshot()
        except (OSError, ValueError, DecisionProviderError) as error:
            return PluginReadiness(False, (str(error),))
        if status.get("state") != "authenticated":
            return PluginReadiness(
                False, (str(status.get("message") or "Claude 客户端尚未登录"),)
            )
        return PluginReadiness(True)

    control.start()
    return PluginSpec("decision_provider", "claude", "通过 Claude 客户端进行结构化 Agent 决策，支持独立登录及升级。", str(context.module_path), factory, configuration, control.teardown, readiness_callback=readiness, controls=control.controls, network_routes_callback=control.network_routes)
