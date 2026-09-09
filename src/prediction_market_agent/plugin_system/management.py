from __future__ import annotations

from typing import Any

from .managed_config import ManagedRuntimeConfig, PLUGIN_KINDS, save_managed_config
from .discovery import PluginCatalog, load_plugin_catalog
from .config import PluginDirectoryConfig


class PluginManagementService:
    """UI-facing plugin-management service; plugin storage remains opaque callback behavior."""

    def __init__(self, config: Any, catalog: PluginCatalog | None = None):
        self.config = config
        self.catalog = catalog or load_plugin_catalog(config)

    def _managed(self) -> ManagedRuntimeConfig:
        return ManagedRuntimeConfig.load(self.config.management_file)

    def _fallback(self, kind: str) -> tuple[str, ...]:
        return {
            "api": self.config.market_api_plugins,
            "decision_provider": self.config.decision_providers,
            "decision_strategy": (),
            "research_tool": self.config.research_tool_plugins,
            "risk": self.config.risk_plugins,
            "hook": self.config.hook_plugins,
        }[kind]

    def manifest(self) -> dict[str, Any]:
        managed = self._managed()
        selected = {
            kind: [
                name
                for name in managed.selected(kind, self._fallback(kind))
                if name in self.catalog.discovered_names(kind)
            ]
            for kind in PLUGIN_KINDS
        }
        strategy = managed.decision_strategy or self.config.decision_strategy_name
        if strategy not in self.catalog.discovered_names("decision_strategy"):
            strategy = ""
        result: dict[str, list[dict[str, Any]]] = {}
        for kind in PLUGIN_KINDS:
            order = selected[kind]
            result[kind] = []
            for plugin_file in self.catalog.files(kind):
                enabled_now = (
                    plugin_file.name == strategy
                    if kind == "decision_strategy"
                    else plugin_file.name in order
                )
                item = (
                    self.catalog.get(kind, plugin_file.name).manifest()
                    if enabled_now
                    else plugin_file.manifest()
                )
                item["initialized"] = enabled_now
                item["enabled"] = enabled_now
                item["priority"] = (
                    0 if kind == "decision_strategy" else order.index(plugin_file.name) + 1
                ) if item["enabled"] else None
                result[kind].append(item)
        return {
            "plugin_directories": PluginDirectoryConfig.load(
                self.config.plugin_directories_file
            ).manifest(),
            "management_storage": str(self.config.management_file.expanduser().resolve()),
            "management_source": str(managed.source_path or managed.path),
            "restart_required_after_change": True,
            "enabled": selected,
            "decision_strategy": strategy,
            "plugins": result,
        }

    def save_plugin_configuration(
        self, kind: str, name: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        spec = self.catalog.get(kind, name)
        if spec.configuration is None:
            raise ValueError(f"Plugin {kind}:{name} has no private configuration")
        spec.configuration.save(values)
        return spec.configuration.manifest()

    def delete_plugin_configuration(self, kind: str, name: str) -> dict[str, Any]:
        spec = self.catalog.get(kind, name)
        if spec.configuration is None:
            raise ValueError(f"Plugin {kind}:{name} has no private configuration")
        spec.configuration.delete()
        return spec.configuration.manifest()

    def reset_plugin_configuration_fields(
        self, kind: str, name: str, fields: list[str]
    ) -> dict[str, Any]:
        spec = self.catalog.get(kind, name)
        if spec.configuration is None:
            raise ValueError(f"Plugin {kind}:{name} has no private configuration")
        spec.configuration.reset(fields)
        return spec.configuration.manifest()

    def save_plugin_directories(self, categories: dict[str, object]) -> dict[str, Any]:
        return PluginDirectoryConfig.save(
            self.config.plugin_directories_file, categories
        ).manifest()

    def reset_plugin_directories(self) -> dict[str, Any]:
        return PluginDirectoryConfig.reset(
            self.config.plugin_directories_file
        ).manifest()

    def save_enabled(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = payload.get("enabled")
        if not isinstance(enabled, dict):
            raise ValueError("enabled must be an object")
        normalized: dict[str, list[str]] = {}
        for kind in PLUGIN_KINDS:
            values = enabled.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"enabled.{kind} must be a list of plugin names")
            names = [item.strip().lower() for item in values if item.strip()]
            if len(names) != len(set(names)):
                raise ValueError(f"enabled.{kind} contains duplicate plugin names")
            for name in names:
                if name not in self.catalog.discovered_names(kind):
                    available = ", ".join(self.catalog.discovered_names(kind)) or "NONE"
                    raise ValueError(
                        f"Unknown {kind} plugin {name!r}; discovered files: {available}"
                    )
            normalized[kind] = names
        if not normalized["api"]:
            raise ValueError("At least one API plugin must be enabled")
        if not normalized["decision_provider"]:
            raise ValueError("At least one decision provider must be enabled")
        strategy = str(payload.get("decision_strategy", "")).strip().lower()
        if strategy not in self.catalog.discovered_names("decision_strategy"):
            raise ValueError(f"Unknown decision strategy plugin: {strategy!r}")
        normalized["decision_strategy"] = [strategy]
        save_managed_config(
            self.config.management_file,
            {"enabled": normalized, "decision_strategy": strategy},
        )
        self.refresh()
        return self.manifest()

    def refresh(self) -> dict[str, Any]:
        self.catalog.shutdown()
        self.catalog = load_plugin_catalog(self.config)
        return self.manifest()

    def shutdown(self) -> None:
        self.catalog.shutdown()
