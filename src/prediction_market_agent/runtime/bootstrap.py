from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from ..agent.decision import make_provider
from ..agent.market_discovery import BuiltInMarketDiscovery
from ..agent.strategy import BuiltInDecisionStrategy
from ..core.config import Config
from ..core.domain import AccountState
from ..core.hooks import HookManager
from ..core.risk import (
    PortfolioRiskContribution,
    RiskCoordinator,
    UnrestrictedExecutionRiskControl,
    UnrestrictedGlobalRiskControl,
)
from ..core.state import StateStore
from ..plugin_system.contracts import PredictionMarketApiPlugin, platform_state_path
from ..plugin_system.discovery import PluginCatalog, load_plugin_catalog
from ..plugin_system.registry import ApiPluginRegistry, load_api_plugins
from .broker import ExecutionGateway


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
    hooks: HookManager
    hook_plugin_status: dict[str, Any]
    risk: RiskCoordinator
    api_registry: ApiPluginRegistry
    platforms: dict[str, PlatformRuntime]
    global_risk: Any
    provider: Any
    research_contributions: list[Any]
    discovery_strategy: Any
    strategy_evolution: bool


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

        hooks = HookManager()
        hook_plugin_status = {}
        for name in config.hook_plugins:
            try:
                hook_plugin_status[name] = catalog.get("hook", name).factory(
                    config, {"hooks": hooks}
                )
            except Exception as error:
                LOGGER.warning("optional hook %s could not start: %s", name, error)
        risk = RiskCoordinator()
        api_registry, plugins = load_api_plugins(config, catalog)
        risk_services: dict[str, Any] = {
            "phase": "bootstrap",
            "api_names": tuple(plugin.name for plugin in plugins),
        }
        risk_products: list[tuple[str, Any]] = []
        portfolio_contributions: list[Any] = []
        for name in config.risk_plugins:
            try:
                created = catalog.get("risk", name).factory(config, risk_services)
            except Exception as error:
                LOGGER.warning("optional risk plugin %s could not start: %s", name, error)
                continue
            risk_products.append((name, created))
            if isinstance(created, PortfolioRiskContribution):
                portfolio_contributions.append(created)
        if len(portfolio_contributions) > 1:
            raise ValueError(
                "At most one enabled risk plugin may provide account allocation and "
                "account/global execution risk controls"
            )

        portfolio_contribution = (
            portfolio_contributions[0] if portfolio_contributions else None
        )
        allocations = (
            portfolio_contribution.initial_allocations(
                tuple(plugin.name for plugin in plugins)
            )
            if portfolio_contribution is not None
            else {plugin.name: None for plugin in plugins}
        )
        multiple = len(plugins) > 1
        platforms: dict[str, PlatformRuntime] = {}
        for plugin in plugins:
            state_path = platform_state_path(config.state_file, plugin.name, multiple)
            store = StateStore(state_path, allocations[plugin.name])
            state = store.load()
            account_risk = (
                portfolio_contribution.create_account_engine(plugin.name, state)
                if portfolio_contribution is not None
                else UnrestrictedExecutionRiskControl(plugin.name, state)
            )
            gateway = plugin.create_write_gateway(state, account_risk)
            gateway.hooks = hooks
            platforms[plugin.name] = PlatformRuntime(
                plugin=plugin,
                store=store,
                state=state,
                gateway=gateway,
            )
            risk.register(plugin.network_rule_engine)
            risk.register(gateway.risk)

        global_risk = (
            portfolio_contribution.create_global_engine(
                {name: item.state for name, item in platforms.items()}
            )
            if portfolio_contribution is not None
            else UnrestrictedGlobalRiskControl()
        )
        risk.register(global_risk)
        risk_services.update(
            {
                "phase": "running",
                "states": {name: item.state for name, item in platforms.items()},
                "platforms": platforms,
            }
        )
        for _name, created in risk_products:
            if created is portfolio_contribution:
                continue
            engines = created if isinstance(created, (list, tuple)) else (created,)
            for engine in engines:
                risk.register(engine)

        return EngineComponents(
            plugin_catalog=catalog,
            decision_strategy=decision_strategy,
            hooks=hooks,
            hook_plugin_status=hook_plugin_status,
            risk=risk,
            api_registry=api_registry,
            platforms=platforms,
            global_risk=global_risk,
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
