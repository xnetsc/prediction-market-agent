from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent.decision import make_provider
from ..core.config import Config
from ..core.domain import AccountState
from ..core.hooks import HookManager
from ..core.risk import PortfolioRiskContribution, RiskCoordinator
from ..core.state import StateStore
from ..sdk.contracts import PredictionMarketApiPlugin, platform_state_path
from ..sdk.discovery import PluginCatalog, load_plugin_catalog
from ..sdk.registry import ApiPluginRegistry, load_api_plugins
from .broker import ExecutionGateway


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


def bootstrap_engine(config: Config) -> EngineComponents:
    """Initialize enabled plugins and wire their runtime services."""
    catalog = load_plugin_catalog(config)
    try:
        strategy_name = config.decision_strategy_name.strip().lower()
        if not strategy_name:
            raise ValueError("An enabled decision strategy plugin must be selected")
        decision_strategy = catalog.get(
            "decision_strategy", strategy_name
        ).factory(config)

        hooks = HookManager()
        hook_plugin_status = {
            name: catalog.get("hook", name).factory(config, {"hooks": hooks})
            for name in config.hook_plugins
        }
        risk = RiskCoordinator()
        api_registry, plugins = load_api_plugins(config, catalog)
        risk_services: dict[str, Any] = {
            "phase": "bootstrap",
            "api_names": tuple(plugin.name for plugin in plugins),
        }
        risk_products: list[tuple[str, Any]] = []
        portfolio_contributions: list[Any] = []
        for name in config.risk_plugins:
            created = catalog.get("risk", name).factory(config, risk_services)
            risk_products.append((name, created))
            if isinstance(created, PortfolioRiskContribution):
                portfolio_contributions.append(created)
        if len(portfolio_contributions) != 1:
            raise ValueError(
                "Exactly one enabled risk plugin must provide account allocation and "
                "account/global execution risk controls"
            )

        portfolio_contribution = portfolio_contributions[0]
        allocations = portfolio_contribution.initial_allocations(
            tuple(plugin.name for plugin in plugins)
        )
        multiple = len(plugins) > 1
        platforms: dict[str, PlatformRuntime] = {}
        for plugin in plugins:
            state_path = platform_state_path(config.state_file, plugin.name, multiple)
            store = StateStore(state_path, allocations[plugin.name])
            state = store.load()
            account_risk = portfolio_contribution.create_account_engine(
                plugin.name, state
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

        global_risk = portfolio_contribution.create_global_engine(
            {name: item.state for name, item in platforms.items()}
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
            research_contributions=[
                catalog.get("research_tool", name).factory(config)
                for name in config.research_tool_plugins
            ],
        )
    except Exception:
        catalog.shutdown()
        raise
