from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from ..agent.decision import make_provider
from ..agent.market_discovery import BuiltInMarketDiscovery
from ..agent.strategy import BuiltInDecisionStrategy
from ..core.config import Config
from ..core.domain import AccountState
from ..core.risk import (
    RiskCoordinator,
)
from ..core.state import StateStore
from ..plugin_system.contracts import PredictionMarketApiPlugin, platform_state_path
from ..plugin_system.discovery import PluginCatalog, load_plugin_catalog
from ..plugin_system.registry import ApiPluginRegistry, load_api_plugins
from .broker import ExecutionGateway
from .market_guard import GuardedMarketApi


LOGGER = logging.getLogger(__name__)


@dataclass
class PlatformRuntime:
    plugin: PredictionMarketApiPlugin
    store: StateStore
    state: AccountState
    gateway: ExecutionGateway


@dataclass
class EngineComponents:
    plugin_catalog: PluginCatalog
    decision_strategy: Any
    risk: RiskCoordinator
    api_registry: ApiPluginRegistry
    platforms: dict[str, PlatformRuntime]
    provider: Any
    research_contributions: list[Any]
    discovery_strategy: Any
    strategy_evolution: bool


def _reported_funds(plugin: Any) -> float | None:
    """Open a new account's ledger at whatever the platform says it can spend.

    A plugin that cannot answer must not take the robot down over it - the ledger simply opens
    empty, which is honest and visible, rather than guessing a figure nobody stated.
    """
    try:
        return float(plugin.account_funds().available)
    except Exception:
        LOGGER.exception("%s could not report its funds; opening the ledger empty", plugin.name)
        return None


def bootstrap_engine(
    config: Config, *, catalog: PluginCatalog | None = None
) -> EngineComponents:
    """Initialize enabled plugins and wire their runtime services."""
    owns_catalog = catalog is None
    catalog = catalog or load_plugin_catalog(config)
    try:
        strategy_name = config.decision_strategy_name.strip().lower()
        decision_strategy = BuiltInDecisionStrategy()
        if strategy_name:
            try:
                decision_strategy = catalog.get(
                    "decision_strategy", strategy_name
                ).factory(config)
            except Exception:
                LOGGER.exception(
                    "optional decision strategy %s could not start; using the built-in strategy",
                    strategy_name,
                )

        risk = RiskCoordinator()
        api_registry, plugins = load_api_plugins(config, catalog)
        risk_services: dict[str, Any] = {
            "phase": "bootstrap",
            "api_names": tuple(plugin.name for plugin in plugins),
        }
        risk_products: list[tuple[str, Any]] = []
        # Business risk and agent policy are separate categories because they answer different
        # questions about the same action: whether the money is within limits, and whether the
        # Agent was allowed to ask for it at all. Both register with the one coordinator, so a
        # trade the model proposes is checked by each in turn.
        for kind, names in (
            ("risk", config.risk_plugins),
            ("agent_policy", config.agent_policy_plugins),
        ):
            for name in names:
                try:
                    created = catalog.get(kind, name).factory(config, risk_services)
                except Exception as error:
                    LOGGER.warning("optional %s plugin %s could not start: %s", kind, name, error)
                    continue
                risk_products.append((name, created))

        multiple = len(plugins) > 1
        platforms: dict[str, PlatformRuntime] = {}
        # Business risk applies to what the platform is asked to do, so the guard sits on the
        # plugin itself rather than inside the Agent path: reads, writes, settlement sweeps and
        # manually triggered cycles all go through the same door.
        for plugin in [GuardedMarketApi(item, risk) for item in plugins]:
            state_path = platform_state_path(config.state_file, plugin.name, multiple)
            store = StateStore(state_path, _reported_funds(plugin))
            state = store.load()
            gateway = plugin.create_write_gateway(state)
            platforms[plugin.name] = PlatformRuntime(
                plugin=plugin,
                store=store,
                state=state,
                gateway=gateway,
            )

        risk_services.update(
            {
                "phase": "running",
                "states": {name: item.state for name, item in platforms.items()},
                "platforms": platforms,
            }
        )
        for _name, created in risk_products:
            engines = created if isinstance(created, (list, tuple)) else (created,)
            for engine in engines:
                risk.register(engine)

        return EngineComponents(
            plugin_catalog=catalog,
            decision_strategy=decision_strategy,
            risk=risk,
            api_registry=api_registry,
            platforms=platforms,
            provider=make_provider(config, catalog),
            research_contributions=_optional_research(config, catalog),
            discovery_strategy=_discovery_strategy(config, catalog),
            strategy_evolution=bool(config.strategy_evolution),
        )
    except Exception:
        if owns_catalog:
            catalog.shutdown()
        raise


def _discovery_strategy(config: Config, catalog: PluginCatalog) -> Any:
    """First ready discovery plugin, else the framework's own strategy.

    The built-in is never registered as a plugin: its text is a runtime constraint rather than
    operator configuration, so it has no configuration surface to expose.
    """
    for name in config.market_discovery_plugins:
        try:
            return catalog.get("market_discovery", name).factory(config)
        except Exception as error:
            LOGGER.warning(
                "optional market discovery plugin %s could not start; using the built-in "
                "strategy: %s",
                name,
                error,
            )
    return BuiltInMarketDiscovery()


def _optional_research(config: Config, catalog: PluginCatalog) -> list[Any]:
    contributions = []
    for name in config.research_tool_plugins:
        try:
            contributions.append(catalog.get("research_tool", name).factory(config))
        except Exception as error:
            LOGGER.warning("optional research plugin %s could not start: %s", name, error)
    return contributions
