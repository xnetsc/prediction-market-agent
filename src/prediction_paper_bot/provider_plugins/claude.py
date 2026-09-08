from __future__ import annotations

import json
import subprocess
from typing import Any

from prediction_paper_bot.decision import DecisionProviderError, StructuredResult
from prediction_paper_bot.plugin_config_io import json_file_callbacks, resolve_plugin_proxy
from prediction_paper_bot.plugins.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginSpec
from prediction_paper_bot.provider_plugins._shared import resolve_executable, subprocess_environment


class ClaudeCliBackend:
    name = "claude"

    def __init__(self, executable: str, model: str, proxy: str, timeout: int):
        self.executable = resolve_executable(executable)
        self.model = model
        self.proxy = proxy
        self.timeout = timeout

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        del schema_name
        command = [self.executable, "-p", "--no-session-persistence", "--permission-mode", "plan", "--max-turns", "1", "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":"))]
        if self.model:
            command.extend(["--model", self.model])
        try:
            completed = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=self.timeout, env=subprocess_environment(self.proxy), check=False)
        except subprocess.TimeoutExpired as error:
            raw = json.dumps({"timeout": True, "stdout": error.stdout or "", "stderr": error.stderr or ""}, ensure_ascii=False)
            raise DecisionProviderError(f"Claude timed out after {self.timeout}s", raw) from error
        raw = json.dumps({"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
        if completed.returncode != 0:
            raise DecisionProviderError(f"Claude failed: {completed.stderr[-1000:]}", raw)
        try:
            value = json.loads(completed.stdout)["structured_output"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise DecisionProviderError("Claude returned invalid structured output", raw) from error
        if not isinstance(value, dict):
            raise DecisionProviderError("Claude returned non-object structured output", raw)
        return StructuredResult(value, raw)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "claude.json")
    configuration = PluginConfiguration(fields=(
        PluginConfigField("CLAUDE_CLI_PATH", "Claude 命令", "string", "本机 Claude CLI 的命令名或可执行文件绝对路径。", required=True),
        PluginConfigField("CLAUDE_MODEL", "Claude 模型", "string", "Claude 客户端使用的模型覆盖；空字符串表示使用客户端默认模型。"),
        PluginConfigField("CLAUDE_HTTP_PROXY", "HTTP 代理", "string", "Claude 子进程独立使用的代理；DIRECT、SYSTEM 或 http(s) URL。", required=True),
        PluginConfigField("CLAUDE_TIMEOUT_SECONDS", "超时秒数", "integer", "每次 Claude 结构化调用的超时时间。", required=True),
    ), load_callback=load, save_callback=save, storage=storage)

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["CLAUDE_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CLAUDE_TIMEOUT_SECONDS must be at least 10")
        return ClaudeCliBackend(values["CLAUDE_CLI_PATH"].strip(), values["CLAUDE_MODEL"].strip(), resolve_plugin_proxy(values["CLAUDE_HTTP_PROXY"], field_name="CLAUDE_HTTP_PROXY"), timeout)

    return PluginSpec("decision_provider", "claude", "通过本机 Claude 客户端进行结构化 Agent 决策。", str(context.module_path), factory, configuration, lambda: None)
