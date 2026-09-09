from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..plugin_system.config import PluginDirectoryConfig
from ..plugin_system.managed_config import ManagedRuntimeConfig, atomic_write_text


DEFAULT_APPLICATION_CONFIG = Path("config/application.json")


@dataclass(frozen=True)
class ApplicationConfigField:
    name: str
    label: str
    field_type: str
    description: str
    default: str | int
    minimum: int | None = None
    maximum: int | None = None
    options: tuple[str, ...] = ()

    def validate(self, value: Any) -> str | int:
        if self.field_type in {"string", "enum"}:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Application setting {self.name} must be non-empty text")
            normalized: str | int = value.strip()
            if self.field_type == "enum" and normalized not in self.options:
                raise ValueError(
                    f"Application setting {self.name} must be one of: "
                    + ", ".join(self.options)
                )
            return normalized
        if self.field_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Application setting {self.name} must be an integer")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(
                    f"Application setting {self.name} must be at least {self.minimum}"
                )
            if self.maximum is not None and value > self.maximum:
                raise ValueError(
                    f"Application setting {self.name} must be at most {self.maximum}"
                )
            return value
        raise ValueError(f"Unsupported application setting type: {self.field_type}")

    def manifest(self, value: str | int, configured: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.field_type,
            "description": self.description,
            "default": self.default,
            "value": value,
            "configured": configured,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "options": list(self.options),
        }


APPLICATION_FIELDS = (
    ApplicationConfigField(
        "working_directory", "机器人工作目录", "string",
        "相对配置、插件私有文件和运行数据的基准目录。", ".",
    ),
    ApplicationConfigField(
        "management_file", "插件启用状态文件", "string",
        "保存插件启用、禁用、优先级和当前决策策略的 JSON 文件；相对路径以机器人工作目录为准。",
        "bot_management.json",
    ),
    ApplicationConfigField(
        "plugin_directories_file", "插件目录文件", "string",
        "保存六类插件扫描目录的 JSON 文件；文件不存在时使用安装包内置目录，界面保存后创建该文件。",
        "config/plugin_directories.json",
    ),
    ApplicationConfigField(
        "max_topics_per_cycle", "每平台每轮候选主题上限", "integer",
        "每个平台在一个决策周期内最多加载的候选主题数。", 10, 1, 1000,
    ),
    ApplicationConfigField(
        "max_decisions_per_cycle", "每平台每轮决策上限", "integer",
        "每个平台在一个决策周期内最多交给 Agent 的市场数。", 6, 1, 1000,
    ),
    ApplicationConfigField(
        "interval_seconds", "连续运行间隔（秒）", "integer",
        "run 命令完成一轮后等待到下一轮的时间。", 60, 10, 86400,
    ),
    ApplicationConfigField(
        "run_until_epoch", "停止时间戳", "integer",
        "Unix 秒级时间戳；0 表示持续运行，非零值到达后结束循环。", 0, 0,
    ),
    ApplicationConfigField(
        "state_file", "账户状态文件", "string",
        "已确认远端结果的本地账户镜像路径；多平台会自动加入平台后缀。", "agent_state.json",
    ),
    ApplicationConfigField(
        "session_db", "决策数据库", "string",
        "保存会话、工具步骤、动作和决策台账的 SQLite 文件路径。", "agent_sessions.sqlite3",
    ),
    ApplicationConfigField(
        "auth_db", "管理员认证数据库", "string",
        "保存 admin Passkey 公钥、签名计数器、认证挑战和登录会话的 SQLite 文件路径。", "admin_auth.sqlite3",
    ),
    ApplicationConfigField(
        "admin_session_hours", "管理员会话时长（小时）", "integer",
        "管理员连续无操作的闲置失效时长；有效请求会刷新该计时。", 72, 1, 720,
    ),
    ApplicationConfigField(
        "admin_absolute_session_hours", "管理员会话绝对时长（小时）", "integer",
        "从 Passkey 登录时刻计算的绝对上限，到期后无论是否持续操作都必须重新登录。", 168, 1, 720,
    ),
    ApplicationConfigField(
        "agent_max_tool_steps", "Agent 工具步骤上限", "integer",
        "每次最终决策前允许 Agent 自主调用研究工具的最大次数。", 4, 0, 12,
    ),
    ApplicationConfigField(
        "agent_tool_result_chars", "单次工具结果字符上限", "integer",
        "单个研究工具结果进入 Provider 上下文的最大字符数。", 12000, 1000, 1000000,
    ),
    ApplicationConfigField(
        "context_window_chars", "Provider 上下文字符预算", "integer",
        "发送给决策 Provider 的组合上下文字符预算。", 60000, 4000, 10000000,
    ),
    ApplicationConfigField(
        "history_per_market", "每市场历史召回条数", "integer",
        "自动加入当前市场决策上下文的历史记录数量。", 12, 0, 10000,
    ),
    ApplicationConfigField(
        "dashboard_host", "管理界面监听地址", "enum",
        "管理与审计界面的监听地址；本地默认回环，容器部署使用 0.0.0.0。", "127.0.0.1",
        options=("127.0.0.1", "localhost", "::1", "0.0.0.0"),
    ),
    ApplicationConfigField(
        "dashboard_port", "管理界面端口", "integer",
        "serve 命令监听的 TCP 端口；修改后需重启。", 8765, 1, 65535,
    ),
    ApplicationConfigField(
        "dashboard_refresh_seconds", "界面刷新间隔（秒）", "integer",
        "决策和运行状态在浏览器中的自动刷新间隔。", 5, 1, 300,
    ),
)

_FIELD_MAP = {field.name: field for field in APPLICATION_FIELDS}


class ApplicationConfigStore:
    """Typed, UI-manageable application settings stored as JSON overrides."""

    def __init__(self, path: Path = DEFAULT_APPLICATION_CONFIG):
        self.path = path.expanduser().resolve()

    def _read_overrides(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Application configuration is invalid JSON: {self.path}: {error}"
            ) from error
        if not isinstance(document, dict):
            raise ValueError("Application configuration must be a JSON object")
        unknown_document = sorted(set(document) - {"version", "values"})
        if unknown_document:
            raise ValueError(
                "Application configuration has unknown sections: "
                + ", ".join(unknown_document)
            )
        values = document.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("Application configuration values must be an object")
        unknown = sorted(set(values) - set(_FIELD_MAP))
        if unknown:
            raise ValueError(
                "Application configuration has unknown fields: " + ", ".join(unknown)
            )
        return {name: _FIELD_MAP[name].validate(value) for name, value in values.items()}

    def values(self) -> dict[str, str | int]:
        overrides = self._read_overrides()
        return {
            field.name: overrides.get(field.name, field.default)
            for field in APPLICATION_FIELDS
        }

    def manifest(self) -> dict[str, Any]:
        overrides = self._read_overrides()
        effective = self.values()
        return {
            "storage": str(self.path),
            "exists": self.path.exists(),
            "restart_required_after_change": True,
            "fields": [
                field.manifest(effective[field.name], field.name in overrides)
                for field in APPLICATION_FIELDS
            ],
        }

    def save(self, submitted: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(submitted, dict):
            raise ValueError("Application configuration values must be an object")
        unknown = sorted(set(submitted) - set(_FIELD_MAP))
        if unknown:
            raise ValueError("Unknown application settings: " + ", ".join(unknown))
        values = {
            name: _FIELD_MAP[name].validate(value) for name, value in submitted.items()
        }
        atomic_write_text(
            self.path,
            json.dumps({"version": 1, "values": values}, ensure_ascii=False, indent=2)
            + "\n",
        )
        return self.manifest()

    def reset(self, names: list[str] | None = None) -> dict[str, Any]:
        if names is None:
            if self.path.exists():
                self.path.unlink()
            return self.manifest()
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Application setting names must be a list of strings")
        unknown = sorted(set(names) - set(_FIELD_MAP))
        if unknown:
            raise ValueError("Unknown application settings: " + ", ".join(unknown))
        values = self._read_overrides()
        for name in names:
            values.pop(name, None)
        if values:
            atomic_write_text(
                self.path,
                json.dumps({"version": 1, "values": values}, ensure_ascii=False, indent=2)
                + "\n",
            )
        elif self.path.exists():
            self.path.unlink()
        return self.manifest()


def _runtime_path(value: str, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()


@dataclass(frozen=True)
class Config:
    working_directory: Path = Path(".")
    decision_providers: tuple[str, ...] = ()
    agent_max_tool_steps: int = 4
    agent_tool_result_chars: int = 12_000
    context_window_chars: int = 60_000
    history_per_market: int = 12
    session_db: Path = Path("agent_sessions.sqlite3")
    auth_db: Path = Path("admin_auth.sqlite3")
    admin_session_hours: int = 72
    admin_absolute_session_hours: int = 168
    max_topics_per_cycle: int = 10
    max_decisions_per_cycle: int = 6
    interval_seconds: int = 60
    run_until_epoch: int = 0
    state_file: Path = Path("agent_state.json")
    application_config_file: Path = DEFAULT_APPLICATION_CONFIG
    market_api_plugins: tuple[str, ...] = ()
    research_tool_plugins: tuple[str, ...] = ()
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_refresh_seconds: int = 5
    management_file: Path = Path("bot_management.json")
    plugin_directories_file: Path = Path("config/plugin_directories.json")
    risk_plugins: tuple[str, ...] = ()
    hook_plugins: tuple[str, ...] = ()
    decision_strategy_name: str = ""

    @classmethod
    def load(cls, path: Path = DEFAULT_APPLICATION_CONFIG) -> "Config":
        store = ApplicationConfigStore(path)
        values = store.values()
        working_directory = _runtime_path(str(values["working_directory"]))
        management_file = _runtime_path(
            str(values["management_file"]), working_directory
        )
        managed = ManagedRuntimeConfig.load(management_file)
        cfg = cls(
            working_directory=working_directory,
            decision_providers=managed.selected("decision_provider", ()),
            agent_max_tool_steps=int(values["agent_max_tool_steps"]),
            agent_tool_result_chars=int(values["agent_tool_result_chars"]),
            context_window_chars=int(values["context_window_chars"]),
            history_per_market=int(values["history_per_market"]),
            session_db=_runtime_path(str(values["session_db"]), working_directory),
            admin_session_hours=int(values["admin_session_hours"]),
            admin_absolute_session_hours=int(values["admin_absolute_session_hours"]),
            max_topics_per_cycle=int(values["max_topics_per_cycle"]),
            max_decisions_per_cycle=int(values["max_decisions_per_cycle"]),
            interval_seconds=int(values["interval_seconds"]),
            run_until_epoch=int(values["run_until_epoch"]),
            state_file=_runtime_path(str(values["state_file"]), working_directory),
            application_config_file=store.path,
            market_api_plugins=managed.selected("api", ()),
            research_tool_plugins=managed.selected("research_tool", ()),
            dashboard_host=str(values["dashboard_host"]),
            dashboard_port=int(values["dashboard_port"]),
            dashboard_refresh_seconds=int(values["dashboard_refresh_seconds"]),
            management_file=management_file,
            plugin_directories_file=_runtime_path(
                str(values["plugin_directories_file"]), working_directory
            ),
            risk_plugins=managed.selected("risk", ()),
            hook_plugins=managed.selected("hook", ()),
            decision_strategy_name=managed.decision_strategy,
            auth_db=_runtime_path(str(values["auth_db"]), working_directory),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.interval_seconds < 10:
            raise ValueError("interval_seconds must be at least 10")
        if not self.decision_providers:
            raise ValueError("At least one decision provider plugin must be enabled")
        if not self.market_api_plugins:
            raise ValueError("At least one API plugin must be enabled")
        if not self.decision_strategy_name:
            raise ValueError("One decision strategy plugin must be selected")
        if self.dashboard_host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
            raise ValueError("dashboard_host must be a supported listen address")
        if not 1 <= self.dashboard_port <= 65535:
            raise ValueError("dashboard_port must be in [1, 65535]")
        if not 1 <= self.dashboard_refresh_seconds <= 300:
            raise ValueError("dashboard_refresh_seconds must be in [1, 300]")
        PluginDirectoryConfig.load(self.plugin_directories_file)
        if not 0 <= self.agent_max_tool_steps <= 12:
            raise ValueError("agent_max_tool_steps must be in [0, 12]")
        if self.agent_tool_result_chars < 1000:
            raise ValueError("agent_tool_result_chars must be at least 1000")
        if self.context_window_chars < 4_000:
            raise ValueError("context_window_chars must be at least 4000")
        if self.history_per_market < 0:
            raise ValueError("history_per_market cannot be negative")
        if not 1 <= self.admin_session_hours <= 720:
            raise ValueError("admin_session_hours must be in [1, 720]")
        if not self.admin_session_hours <= self.admin_absolute_session_hours <= 720:
            raise ValueError(
                "admin_absolute_session_hours must be between admin_session_hours and 720"
            )
        if self.max_topics_per_cycle < 1:
            raise ValueError("max_topics_per_cycle must be positive")
        if self.max_decisions_per_cycle < 1:
            raise ValueError("max_decisions_per_cycle must be positive")
