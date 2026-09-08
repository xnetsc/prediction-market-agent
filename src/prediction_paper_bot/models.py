from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv as _load_dotenv
from .managed_config import ManagedRuntimeConfig
from .sdk_config import DEFAULT_SDK_CONFIG, PluginSdkConfig

def load_dotenv() -> Path | None:
    """Load one local .env with python-dotenv and preserve process-level overrides."""
    explicit = os.getenv("PAPER_ENV_FILE", "").strip()
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        project_root = Path(__file__).resolve().parents[2]
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
        raise FileNotFoundError(f"PAPER_ENV_FILE does not exist: {candidates[0]}")
    return None


def provider_priority() -> tuple[str, ...]:
    raw = os.getenv("DECISION_PROVIDERS", "").split(",")
    result: list[str] = []
    for item in raw:
        name = item.strip().lower()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def api_plugin_names() -> tuple[str, ...]:
    configured = os.getenv("MARKET_API_PLUGINS", "")
    result: list[str] = []
    for item in configured.split(","):
        name = item.strip().lower()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def research_tool_plugin_names() -> tuple[str, ...]:
    configured = os.getenv("RESEARCH_TOOL_PLUGINS", "")
    return tuple(
        dict.fromkeys(
            item.strip().lower() for item in configured.split(",") if item.strip()
        )
    )


@dataclass(frozen=True)
class Config:
    decision_providers: tuple[str, ...] = ()
    agent_max_tool_steps: int = 4
    agent_tool_result_chars: int = 12_000
    context_window_chars: int = 60_000
    history_per_market: int = 12
    session_db: Path = Path("paper_sessions.sqlite3")
    max_topics_per_cycle: int = 10
    max_decisions_per_cycle: int = 6
    interval_seconds: int = 60
    run_until_epoch: int = 0
    state_file: Path = Path("paper_state.json")
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
        providers = managed.selected("decision_provider", provider_priority())
        api_plugins = managed.selected("api", api_plugin_names())
        cfg = cls(
            decision_providers=providers,
            agent_max_tool_steps=int(os.getenv("AGENT_MAX_TOOL_STEPS", "4")),
            agent_tool_result_chars=int(os.getenv("AGENT_TOOL_RESULT_CHARS", "12000")),
            context_window_chars=int(os.getenv("CONTEXT_WINDOW_CHARS", "60000")),
            history_per_market=int(os.getenv("HISTORY_PER_MARKET", "12")),
            session_db=Path(os.getenv("PAPER_SESSION_DB", "paper_sessions.sqlite3")),
            max_topics_per_cycle=int(os.getenv("PAPER_MAX_TOPICS_PER_CYCLE", "10")),
            max_decisions_per_cycle=int(os.getenv("PAPER_MAX_DECISIONS_PER_CYCLE", "6")),
            interval_seconds=int(os.getenv("PAPER_INTERVAL_SECONDS", "60")),
            run_until_epoch=int(os.getenv("PAPER_RUN_UNTIL_EPOCH", "0")),
            state_file=Path(os.getenv("PAPER_STATE_FILE", "paper_state.json")),
            loaded_env_file=str(loaded_env_file) if loaded_env_file else "",
            market_api_plugins=api_plugins,
            research_tool_plugins=managed.selected(
                "research_tool", research_tool_plugin_names()
            ),
            dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
            dashboard_port=int(os.getenv("DASHBOARD_PORT", "8765")),
            dashboard_refresh_seconds=int(os.getenv("DASHBOARD_REFRESH_SECONDS", "5")),
            management_file=management_file,
            plugin_sdk_config_file=Path(
                os.getenv("PLUGIN_SDK_CONFIG_FILE", str(DEFAULT_SDK_CONFIG))
            ).expanduser().resolve(),
            risk_plugins=managed.selected(
                "risk",
                tuple(
                    item.strip().lower()
                    for item in os.getenv(
                        "RISK_PLUGINS", ""
                    ).split(",")
                    if item.strip()
                ),
            ),
            hook_plugins=managed.selected(
                "hook",
                tuple(
                    item.strip().lower()
                    for item in os.getenv("HOOK_PLUGINS", "").split(",")
                    if item.strip()
                ),
            ),
            decision_strategy_name=managed.decision_strategy,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.interval_seconds < 10:
            raise ValueError("PAPER_INTERVAL_SECONDS must be at least 10")
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
            raise ValueError("PAPER_MAX_DECISIONS_PER_CYCLE must be positive")

@dataclass
class Position:
    token_id: str
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str
    quantity: float
    average_price: float
    mark_price: float
    opened_at: int

    @property
    def market_value(self) -> float:
        return self.quantity * self.mark_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.average_price


@dataclass
class ExecutionOrder:
    order_id: str
    quote_id: str
    token_id: str
    side: str
    order_type: str
    status: str
    quantity: float
    price: float
    notional: float
    fee: float
    fee_bps: int
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str
    created_at: int
    reason: str = ""


@dataclass
class ExecutionQuote:
    quote_id: str
    token_id: str
    side: str
    order_type: str
    quantity: float
    price: float
    notional: float
    fee_bps: int
    expires_at: int
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str


@dataclass
class AccountState:
    starting_capital: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[ExecutionOrder] = field(default_factory=list)
    realized_pnl: float = 0.0
    transferred_out: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    risk_metrics: dict[str, float] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def exposure(self) -> float:
        return sum(position.market_value for position in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.exposure

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["positions"] = {key: asdict(value) for key, value in self.positions.items()}
        data["orders"] = [asdict(value) for value in self.orders]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountState":
        payload = dict(data)
        payload["positions"] = {
            key: Position(**value) for key, value in payload.get("positions", {}).items()
        }
        payload["orders"] = [ExecutionOrder(**value) for value in payload.get("orders", [])]
        return cls(**payload)


class StateStore:
    def __init__(self, path: Path, starting_capital: float):
        self.path = path
        self.starting_capital = starting_capital

    def load(self) -> AccountState:
        if not self.path.exists():
            return AccountState(
                starting_capital=self.starting_capital,
                cash=self.starting_capital,
            )
        with self.path.open("r", encoding="utf-8") as handle:
            state = AccountState.from_dict(json.load(handle))
        if abs(state.starting_capital - self.starting_capital) > 1e-9:
            raise ValueError(
                "Existing state uses a different starting capital; choose a new PAPER_STATE_FILE"
            )
        return state

    def save(self, state: AccountState) -> None:
        state.updated_at = int(time.time() * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
