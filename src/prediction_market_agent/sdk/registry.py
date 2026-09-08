from __future__ import annotations

from collections.abc import Callable

from ..core.config import Config
from .contracts import PredictionMarketApiPlugin
from .discovery import PluginCatalog, load_plugin_catalog


PluginFactory = Callable[[Config], PredictionMarketApiPlugin]


class ApiPluginRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, PluginFactory] = {}

    def register(self, name: str, factory: PluginFactory) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("API plugin name cannot be empty")
        if normalized in self._factories:
            raise ValueError(f"API plugin is already registered: {normalized}")
        self._factories[normalized] = factory

    def create(self, names: tuple[str, ...], config: Config) -> list[PredictionMarketApiPlugin]:
        plugins: list[PredictionMarketApiPlugin] = []
        for name in names:
            factory = self._factories.get(name)
            if factory is None:
                available = ", ".join(sorted(self._factories)) or "NONE"
                raise ValueError(f"Unknown API plugin {name!r}; registered plugins: {available}")
            plugins.append(factory(config))
        return plugins

    @property
    def registered_names(self) -> tuple[str, ...]:
        return tuple(self._factories)


def load_api_plugins(
    config: Config, catalog: PluginCatalog | None = None
) -> tuple[ApiPluginRegistry, list[PredictionMarketApiPlugin]]:
    catalog = catalog or load_plugin_catalog(config)
    registry = ApiPluginRegistry()
    for spec in catalog.specs("api"):
        registry.register(spec.name, spec.factory)
    return registry, registry.create(config.market_api_plugins, config)
