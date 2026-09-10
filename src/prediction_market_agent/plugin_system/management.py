from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .managed_config import (
    ManagedRuntimeConfig,
    PLUGIN_KINDS,
    atomic_write_text,
    save_managed_config,
)
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
        # The built-in default selection file supplies the initial strategy.
        # An explicit empty managed value means "no strategy" and must remain empty.
        strategy = managed.decision_strategy
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
                self.config.plugin_directories_file,
                working_directory=self.config.working_directory,
            ).manifest(),
            "management_storage": str(self.config.management_file.expanduser().resolve()),
            "management_source": str(managed.source_path or managed.path),
            "restart_required_after_change": False,
            "enabled": selected,
            "decision_strategy": strategy,
            "robot_paused": managed.robot_paused,
            "paused_platforms": list(managed.paused_platforms),
            "plugins": result,
        }

    def save_plugin_configuration(
        self, kind: str, name: str, values: dict[str, Any], *, clear_secrets: list[str] | None = None
    ) -> dict[str, Any]:
        spec = self.catalog.get(kind, name)
        if spec.configuration is None:
            raise ValueError(f"Plugin {kind}:{name} has no private configuration")
        spec.configuration.save(values, clear_secrets=clear_secrets)
        return spec.configuration.manifest()

    def control_status(self) -> dict[str, Any]:
        items = []
        for kind, entries in self.manifest()["plugins"].items():
            for entry in entries:
                if entry.get("enabled") and entry.get("has_controls"):
                    spec = self.catalog.get(kind, entry["name"])
                    items.append({"kind": kind, "name": spec.name,
                                  "status": spec.controls.status_callback()})
        return {"items": items}

    def configuration_choices(self, kind: str, name: str, field: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        configuration = self.catalog.get(kind, name).configuration
        if configuration is None or field not in configuration.choice_fields:
            raise ValueError("Plugin field does not provide dynamic choices")
        if configuration.context_choices_callback:
            schema=next(item for item in configuration.fields if item.name==field)
            submitted={} if values is None else values
            if not isinstance(submitted,dict):raise ValueError("Choice context must be an object")
            context={}
            for dependency in schema.choices_depend_on:
                spec=next(item for item in configuration.fields if item.name==dependency)
                if spec.secret:raise ValueError("Choices cannot depend on a secret field")
                if dependency in submitted:context[dependency]=configuration._validate_value(spec,submitted[dependency])
            return {"items": configuration.context_choices_callback(field,context)}
        return {"items": configuration.choices_callback(field)}

    def control_action(self, kind: str, name: str, action: str,
                       values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Control values must be an object")
        spec = self.catalog.get(kind, name)
        if spec.controls is None:
            raise ValueError("Plugin does not expose management controls")
        return spec.controls.action_callback(action, values)

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
            self.config.plugin_directories_file,
            categories,
            working_directory=self.config.working_directory,
        ).manifest()

    def reset_plugin_directories(self) -> dict[str, Any]:
        return PluginDirectoryConfig.reset(
            self.config.plugin_directories_file,
            working_directory=self.config.working_directory,
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
        strategy = str(payload.get("decision_strategy", "")).strip().lower()
        if strategy and strategy not in self.catalog.discovered_names("decision_strategy"):
            raise ValueError(f"Unknown decision strategy plugin: {strategy!r}")
        normalized["decision_strategy"] = [strategy] if strategy else []
        save_managed_config(
            self.config.management_file,
            {
                "enabled": normalized,
                "decision_strategy": strategy,
                "robot_paused": self._managed().robot_paused,
                "paused_platforms": list(self._managed().paused_platforms),
            },
        )
        self.refresh()
        return self.manifest()

    def save_runtime_control(
        self, *, robot_paused: bool, paused_platforms: list[str]
    ) -> dict[str, Any]:
        if not isinstance(robot_paused, bool):
            raise ValueError("robot_paused must be a boolean")
        if not isinstance(paused_platforms, list) or not all(
            isinstance(item, str) for item in paused_platforms
        ):
            raise ValueError("paused_platforms must be a list of platform names")
        known = set(self.catalog.discovered_names("api"))
        normalized = tuple(
            dict.fromkeys(item.strip().lower() for item in paused_platforms if item.strip())
        )
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ValueError("Unknown API platforms: " + ", ".join(unknown))
        managed = self._managed()
        save_managed_config(
            self.config.management_file,
            {
                "enabled": {
                    kind: list(managed.selected(kind, self._fallback(kind)))
                    for kind in PLUGIN_KINDS
                },
                "decision_strategy": managed.decision_strategy,
                "robot_paused": robot_paused,
                "paused_platforms": list(normalized),
            },
        )
        return {
            "robot_paused": robot_paused,
            "paused_platforms": list(normalized),
        }

    def install_plugin(
        self, kind: str, name: str, source: str, target_directory: str
    ) -> dict[str, Any]:
        if kind not in PLUGIN_KINDS:
            raise ValueError(f"Unknown plugin category: {kind!r}")
        normalized = name.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", normalized):
            raise ValueError(
                "Plugin name must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores"
            )
        if normalized in self.catalog.discovered_names(kind):
            raise FileExistsError(f"A {kind} plugin named {normalized!r} is already discovered")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Plugin source cannot be empty")
        try:
            tree = ast.parse(source, filename=f"{normalized}.py")
        except SyntaxError as error:
            raise ValueError(f"Plugin source is invalid Python: {error}") from error
        if not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "initialize_plugin"
            for node in tree.body
        ):
            raise ValueError("Plugin source must define initialize_plugin(context)")
        directories = PluginDirectoryConfig.load(
            self.config.plugin_directories_file,
            working_directory=self.config.working_directory,
        ).directories[kind]
        target_root = Path(target_directory).expanduser().resolve()
        if target_root not in directories:
            raise ValueError("Plugin install target is not configured for this category")
        destination = target_root / f"{normalized}.py"
        if destination.exists():
            raise FileExistsError(f"Plugin file already exists: {destination}")
        target_root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, source.rstrip() + "\n")
        self.refresh()
        return {"installed": str(destination), "management": self.manifest()}

    def refresh(self) -> dict[str, Any]:
        self.catalog.shutdown()
        self.catalog = load_plugin_catalog(self.config)
        managed = self._managed()
        enabled = {
            kind: [
                name
                for name in managed.selected(kind, self._fallback(kind))
                if name in self.catalog.discovered_names(kind)
            ]
            for kind in PLUGIN_KINDS
        }
        strategy = managed.decision_strategy
        if strategy not in self.catalog.discovered_names("decision_strategy"):
            strategy = ""
        enabled["decision_strategy"] = [strategy] if strategy else []
        paused = [
            name
            for name in managed.paused_platforms
            if name in self.catalog.discovered_names("api")
        ]
        normalized = {
            "enabled": enabled,
            "decision_strategy": strategy,
            "robot_paused": managed.robot_paused,
            "paused_platforms": paused,
        }
        if managed.to_dict() != {"version": 1, **normalized}:
            save_managed_config(self.config.management_file, normalized)
        return self.manifest()

    def shutdown(self) -> None:
        self.catalog.shutdown()
