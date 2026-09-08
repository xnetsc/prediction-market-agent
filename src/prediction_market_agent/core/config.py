from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv as _load_dotenv

from ..sdk.config import DEFAULT_SDK_CONFIG, PluginSdkConfig
from ..sdk.managed_config import ManagedRuntimeConfig


def load_dotenv() -> Path | None:
    """Load one local .env while preserving process-level overrides."""
    explicit = os.getenv("PREDICTION_AGENT_ENV_FILE", "").strip()
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        project_root = Path(__file__).resolve().parents[3]
        candidates = [Path.cwd() / ".env", project_root / ".env"]
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        _load_dotenv(dotenv_path=path, override=False, encoding="utf-8-sig")
        return path
    if explicit:
        raise FileNotFoundError(
            f"PREDICTION_AGENT_ENV_FILE does not exist: {candidates[0]}"
        )
    return None


def _ordered_names(variable: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in os.getenv(variable, "").split(",")
            if item.strip()
        )
    )


@dataclass(frozen=True)
class Config:
    decision_providers: tuple[str, ...] = ()
    agent_max_tool_steps: int = 4
    agent_tool_result_chars: int = 12_000
    context_window_chars: int = 60_000
    history_per_market: int = 12
    session_db: Path = Path("agent_sessions.sqlite3")
    max_topics_per_cycle: int = 10
    max_decisions_per_cycle: int = 6
    interval_seconds: int = 60
    run_until_epoch: int = 0
    state_file: Path = Path("agent_state.json")
    loaded_env_file: str = ""
    market_api_plugins: tuple[str, ...] = ()
    research_tool_plugins: tuple[str, ...] = ()
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_refresh_seconds: int = 5
    management_file: Path = Path("bot_management.json")
    plugin_sdk_config_file: Path = DEFAULT_SDK_CONFIG
    risk_plugins: tuple[str, ...] = ()
    hook_plugins: tuple[str, ...] = ()
    decision_strategy_name: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        loaded_env_file = load_dotenv()
        management_file = Path(
            os.getenv("BOT_MANAGEMENT_FILE", "bot_management.json")
        ).expanduser().resolve()
        managed = ManagedRuntimeConfig.load(management_file)
        cfg = cls(
            decision_providers=managed.selected(
                "decision_provider", _ordered_names("DECISION_PROVIDERS")
            ),
            agent_max_tool_steps=int(os.getenv("AGENT_MAX_TOOL_STEPS", "4")),
            agent_tool_result_chars=int(os.getenv("AGENT_TOOL_RESULT_CHARS", "12000")),
            context_window_chars=int(os.getenv("CONTEXT_WINDOW_CHARS", "60000")),
            history_per_market=int(os.getenv("HISTORY_PER_MARKET", "12")),
            session_db=Path(
                os.getenv("PREDICTION_AGENT_SESSION_DB", "agent_sessions.sqlite3")
            ),
            max_topics_per_cycle=int(
                os.getenv("PREDICTION_AGENT_MAX_TOPICS_PER_CYCLE", "10")
            ),
            max_decisions_per_cycle=int(
                os.getenv("PREDICTION_AGENT_MAX_DECISIONS_PER_CYCLE", "6")
            ),
            interval_seconds=int(
                os.getenv("PREDICTION_AGENT_INTERVAL_SECONDS", "60")
            ),
            run_until_epoch=int(
                os.getenv("PREDICTION_AGENT_RUN_UNTIL_EPOCH", "0")
            ),
            state_file=Path(
                os.getenv("PREDICTION_AGENT_STATE_FILE", "agent_state.json")
            ),
            loaded_env_file=str(loaded_env_file) if loaded_env_file else "",
            market_api_plugins=managed.selected(
                "api", _ordered_names("MARKET_API_PLUGINS")
            ),
            research_tool_plugins=managed.selected(
                "research_tool", _ordered_names("RESEARCH_TOOL_PLUGINS")
            ),
            dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8765")),
            dashboard_refresh_seconds=int(
                os.getenv("DASHBOARD_REFRESH_SECONDS", "5")
            ),
            management_file=management_file,
            plugin_sdk_config_file=Path(
                os.getenv("PLUGIN_SDK_CONFIG_FILE", str(DEFAULT_SDK_CONFIG))
            ).expanduser().resolve(),
            risk_plugins=managed.selected("risk", _ordered_names("RISK_PLUGINS")),
            hook_plugins=managed.selected("hook", _ordered_names("HOOK_PLUGINS")),
            decision_strategy_name=managed.decision_strategy,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.interval_seconds < 10:
            raise ValueError("PREDICTION_AGENT_INTERVAL_SECONDS must be at least 10")
        if not self.decision_providers:
            raise ValueError("DECISION_PROVIDERS must contain at least one plugin name")
        if not self.market_api_plugins:
            raise ValueError("MARKET_API_PLUGINS must contain at least one plugin name")
        if not self.decision_strategy_name:
            raise ValueError("One decision strategy plugin must be selected")
        if self.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("DASHBOARD_HOST is restricted to a loopback address")
        if not 1 <= self.dashboard_port <= 65535:
            raise ValueError("DASHBOARD_PORT must be in [1, 65535]")
        if not 1 <= self.dashboard_refresh_seconds <= 300:
            raise ValueError("DASHBOARD_REFRESH_SECONDS must be in [1, 300]")
        PluginSdkConfig.load(self.plugin_sdk_config_file)
        if not 0 <= self.agent_max_tool_steps <= 12:
            raise ValueError("AGENT_MAX_TOOL_STEPS must be in [0, 12]")
        if self.agent_tool_result_chars < 1000:
            raise ValueError("AGENT_TOOL_RESULT_CHARS must be at least 1000")
        if self.context_window_chars < 4_000:
            raise ValueError("CONTEXT_WINDOW_CHARS must be at least 4000")
        if self.history_per_market < 0:
            raise ValueError("HISTORY_PER_MARKET cannot be negative")
        if self.max_decisions_per_cycle < 1:
            raise ValueError(
                "PREDICTION_AGENT_MAX_DECISIONS_PER_CYCLE must be positive"
            )
