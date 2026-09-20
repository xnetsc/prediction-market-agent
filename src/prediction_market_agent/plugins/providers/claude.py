from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult
from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginReadiness, PluginSpec
from prediction_market_agent.plugins.providers._model_catalog import ClientModelCatalog
from prediction_market_agent.plugins.providers._shared import configured_client_homes, resolve_executable, shared_agent_history_instruction, subprocess_environment, subprocess_output_text
from prediction_market_agent.plugins.providers._client_control import ClientControl, client_fields, client_configuration_loader


class ClaudeCliBackend:
    name = "claude"

    def __init__(self, executable: str, model: str, proxy: str, timeout: int, control: ClientControl | None = None, effort: str = "", history_database: Path | None = None, client_homes: dict[str, Path] | None = None):
        self.executable = resolve_executable(executable)
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.control = control
        self.effort = effort
        self.history_database = history_database.resolve() if history_database else None
        self.client_homes = client_homes
        self._sessions = threading.local()

    @contextmanager
    def session(self):
        """One fresh official CLI conversation for one complete application round."""
        if getattr(self._sessions, "root", None) is not None:
            yield
            return
        with tempfile.TemporaryDirectory(prefix="prediction-claude-round-") as directory:
            self._sessions.root = Path(directory)
            self._sessions.session_id = str(uuid.uuid4())
            self._sessions.started = False
            try:
                yield
            finally:
                self._sessions.root = None
                self._sessions.session_id = ""
                self._sessions.started = False

    def _with_history(self, prompt: str) -> str:
        return prompt + shared_agent_history_instruction(
            getattr(self, "history_database", None),
            getattr(self, "client_homes", None),
        )

    def quota(self) -> dict[str, Any] | None:
        """Whether this account can serve a request and, if not, when it can again - for free."""
        return self.control.quota() if self.control else None

    def entitlement(self) -> bool | None:
        """Whether this account can serve a request, asked without spending anything.

        The client knows its own quota and its own session state, so both questions are put to it
        rather than answered by sending a request and seeing what happens. Returns None when the
        client cannot say, which leaves the caller free to find out the expensive way.
        """
        reading = self.quota()
        if reading is None:
            return None
        if reading.get("available") is not None:
            return bool(reading["available"])
        return reading.get("signed_in")

    def liveness(self) -> bool | None:
        """Free signal the client provides about its own session; None when unavailable."""
        return self.control.authenticated() if self.control else None

    STRUCTURED_ANSWER_TURNS = "6"
    """Turns this client may take to produce the one answer being asked of it.

    The framework runs its own tool loop, one invocation per step, so it wants a single structured
    reply here and nothing else. But this CLI counts the model's own internal steps as turns, and a
    prompt worth answering is usually thought about before it is answered. At 1 that thinking was
    the whole budget: the run ended `error_max_turns` with no output, exited non-zero, and reported
    an empty stderr, so every decision this provider was asked for died silently and looked like a
    broken client. The number only has to cover getting to an answer, not to allow wandering.
    """

    @staticmethod
    def _why_it_stopped(stdout: str) -> str:
        try:
            envelope = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return ""
        if not isinstance(envelope, dict):
            return ""
        parts = [str(envelope.get(key, "")) for key in ("subtype", "terminal_reason", "result")]
        return " / ".join(part for part in parts if part)[:1000]

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        del schema_name
        sessions = getattr(self, "_sessions", None)
        active_root = getattr(sessions, "root", None)
        temporary = None if active_root is not None else tempfile.TemporaryDirectory(prefix="prediction-claude-")
        root = active_root or Path(temporary.name)
        try:
            started = bool(getattr(sessions, "started", False))
            session_id = str(getattr(sessions, "session_id", "") or "")
            command = [self.control.executable() if self.control else self.executable, "-p"]
            if active_root is None:
                command.append("--no-session-persistence")
            elif started:
                command.extend(["--resume", session_id])
            else:
                command.extend(["--session-id", session_id])
            command.extend(["--permission-mode", "acceptEdits", "--max-turns", self.STRUCTURED_ANSWER_TURNS, "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":"))])
            if self.model:
                command.extend(["--model", self.model])
            if self.effort:
                command.extend(["--effort", self.effort])
            try:
                completed = subprocess.run(command, input=self._with_history(prompt), text=True, capture_output=True, timeout=self.timeout, env=self.control.environment() if self.control else subprocess_environment(self.proxy), cwd=root, check=False)
            except subprocess.TimeoutExpired as error:
                raw = json.dumps({"timeout": True, "stdout": subprocess_output_text(error.stdout), "stderr": subprocess_output_text(error.stderr)}, ensure_ascii=False)
                raise DecisionProviderError(f"Claude timed out after {self.timeout}s", raw) from error
            raw = json.dumps({"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
            if completed.returncode != 0:
                if self.control:
                    self.control.record_auth_failure(completed.stderr + completed.stdout)
                # This client reports why it stopped in its JSON envelope and leaves stderr empty, so
                # taking stderr alone produced "Claude failed:" and nothing else - a message that says
                # a provider is broken while withholding the one word that says how.
                raise DecisionProviderError(
                    f"Claude failed: {self._why_it_stopped(completed.stdout) or completed.stderr[-1000:]}",
                    raw,
                )
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
            if active_root is not None:
                sessions.started = True
            return StructuredResult(value, raw)
        finally:
            if temporary is not None:
                temporary.cleanup()


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
        history_database = getattr(config, "session_db", None)
        values = configuration.load()
        timeout = int(values["CLAUDE_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CLAUDE_TIMEOUT_SECONDS must be at least 10")
        return ClaudeCliBackend(control.executable(), values["CLAUDE_MODEL"].strip(), control.proxy_settings()["proxy"], timeout, control, values["CLAUDE_EFFORT"].strip(), history_database, configured_client_homes(context.working_directory))

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
