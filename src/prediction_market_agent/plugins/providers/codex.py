from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from contextlib import contextmanager
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
from prediction_market_agent.plugins.providers._shared import configured_client_homes, resolve_executable, shared_agent_history_instruction, subprocess_environment, subprocess_output_text
from prediction_market_agent.plugins.providers._client_control import ClientControl, client_fields, client_configuration_loader


class CodexCliBackend:
    name = "codex"

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
        with tempfile.TemporaryDirectory(prefix="prediction-codex-round-") as directory:
            self._sessions.root = Path(directory)
            self._sessions.thread_id = ""
            try:
                yield
            finally:
                self._sessions.root = None
                self._sessions.thread_id = ""

    def _with_history(self, prompt: str) -> str:
        return prompt + shared_agent_history_instruction(
            self.history_database, getattr(self, "client_homes", None)
        )

    @staticmethod
    def _thread_id(stdout: str) -> str:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return ""

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

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        active_root = getattr(self._sessions, "root", None)
        temporary = None if active_root is not None else tempfile.TemporaryDirectory(prefix="prediction-codex-")
        root = active_root or Path(temporary.name)
        try:
            schema_path = root / f"{schema_name}.schema.json"
            output_path = root / f"{schema_name}.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            output_path.unlink(missing_ok=True)
            executable = self.control.executable() if self.control else self.executable
            thread_id = str(getattr(self._sessions, "thread_id", "") or "")
            if active_root is not None and thread_id:
                command = [
                    executable, "--search", "exec", "resume", thread_id,
                    "--skip-git-repo-check", "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "--json",
                ]
            else:
                command = [
                    executable, "--search", "exec",
                    *([] if active_root is not None else ["--ephemeral"]),
                    "--skip-git-repo-check", "--sandbox", "workspace-write",
                    "--color", "never", "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-C", str(root),
                    *(["--json"] if active_root is not None else []),
                ]
            if self.model:
                command.extend(["--model", self.model])
            if self.effort:
                command.extend(["-c", "model_reasoning_effort="+json.dumps(self.effort)])
            command.append("-")
            try:
                completed = subprocess.run(
                    command, input=self._with_history(prompt), text=True, capture_output=True,
                    timeout=self.timeout,
                    env=self.control.environment() if self.control else subprocess_environment(self.proxy),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raw = json.dumps({"timeout": True, "stdout": subprocess_output_text(error.stdout), "stderr": subprocess_output_text(error.stderr)}, ensure_ascii=False)
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
            if active_root is not None and not thread_id:
                started = self._thread_id(completed.stdout)
                if not started:
                    raise DecisionProviderError("Codex did not report the new round session id", raw)
                self._sessions.thread_id = started
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DecisionProviderError("Codex returned invalid JSON", raw) from error
            if not isinstance(value, dict):
                raise DecisionProviderError("Codex returned non-object JSON", raw)
            return StructuredResult(value, raw)
        finally:
            if temporary is not None:
                temporary.cleanup()


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
        history_database = getattr(config, "session_db", None)
        values = configuration.load()
        timeout = int(values["CODEX_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("CODEX_TIMEOUT_SECONDS must be at least 10")
        return CodexCliBackend(
            control.executable(),
            values["CODEX_MODEL"].strip(),
            control.proxy_settings()["proxy"],
            timeout, control, values["CODEX_EFFORT"].strip(), history_database,
            configured_client_homes(context.working_directory),
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
