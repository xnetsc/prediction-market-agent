from __future__ import annotations

import importlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .managed_config import ManagedRuntimeConfig, PLUGIN_KINDS
from .config import PluginDirectoryConfig


FIELD_TYPES = frozenset({"string", "integer", "number", "boolean", "enum", "secret"})
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
UNSET = object()


@dataclass(frozen=True)
class PluginConfigField:
    """One plugin-owned JSON field used for validation and dynamic UI generation."""

    name: str
    label: str
    field_type: str
    description: str
    required: bool = False
    default: Any = UNSET
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError(f"Plugin configuration field has an invalid name: {self.name!r}")
        if not self.label.strip():
            raise ValueError(f"Plugin configuration field {self.name!r} requires a label")
        if not self.description.strip():
            raise ValueError(
                f"Plugin configuration field {self.name!r} requires a non-empty description"
            )
        if self.field_type not in FIELD_TYPES:
            raise ValueError(
                f"Plugin configuration field {self.name!r} has unsupported type "
                f"{self.field_type!r}"
            )
        if self.field_type == "enum" and not self.options:
            raise ValueError(f"Enum field {self.name!r} requires options")
        if self.field_type != "enum" and self.options:
            raise ValueError(f"Only enum fields may define options: {self.name!r}")

    @property
    def secret(self) -> bool:
        return self.field_type == "secret"

    def manifest(self, value: Any, configured: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.field_type,
            "description": self.description,
            "required": self.required,
            "has_default": self.default is not UNSET,
            "default": None if self.default is UNSET else self.default,
            "options": list(self.options),
            "value": "" if self.secret else value,
            "configured": configured,
            "sensitive": self.secret,
        }


@dataclass(frozen=True)
class PluginConfiguration:
    """Plugin-system-facing schema and plugin-owned JSON-object load/save callbacks."""

    fields: tuple[PluginConfigField, ...]
    load_callback: Callable[[], dict[str, Any]]
    save_callback: Callable[[dict[str, Any]], None]
    delete_callback: Callable[[], None]
    storage: dict[str, Any]
    required: bool = False

    def __post_init__(self) -> None:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("Plugin configuration schema contains duplicate fields")
        if not all(
            callable(callback)
            for callback in (self.load_callback, self.save_callback, self.delete_callback)
        ):
            raise ValueError(
                "Plugin configuration must provide callable load/save/delete functions"
            )
        if not isinstance(self.storage, dict):
            raise ValueError("Plugin configuration storage description must be an object")
        if self.storage.get("kind") != "json_file" or not self.storage.get("location"):
            raise ValueError("Plugin configuration storage must describe one JSON file")

    def load(self) -> dict[str, Any]:
        raw = self.load_callback()
        if not isinstance(raw, dict):
            raise ValueError("Plugin configuration load function must return a JSON object")
        unknown = sorted(set(raw) - {field.name for field in self.fields})
        if unknown:
            raise ValueError(f"Plugin configuration has unknown fields: {', '.join(unknown)}")
        values: dict[str, Any] = {}
        for field in self.fields:
            if field.name in raw:
                values[field.name] = self._validate_value(field, raw[field.name])
            elif field.default is not UNSET:
                values[field.name] = self._validate_value(field, field.default)
            elif field.required:
                raise ValueError(
                    f"Required plugin configuration field is missing: {field.name}"
                )
        return values

    @staticmethod
    def _validate_value(field: PluginConfigField, value: Any) -> Any:
        if field.field_type in {"string", "secret", "enum"}:
            if not isinstance(value, str):
                raise ValueError(f"Plugin field {field.name} must be a string")
            if field.required and not value.strip():
                raise ValueError(f"Plugin field {field.name} cannot be empty")
            if field.field_type == "enum" and value not in field.options:
                raise ValueError(
                    f"Plugin field {field.name} must be one of: {', '.join(field.options)}"
                )
            return value
        if field.field_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"Plugin field {field.name} must be a boolean")
            return value
        if field.field_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Plugin field {field.name} must be an integer")
            return value
        if field.field_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Plugin field {field.name} must be a number")
            return value
        raise AssertionError(field.field_type)

    def save(self, submitted: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(submitted, dict):
            raise ValueError("Plugin configuration values must be an object")
        existing_raw = self.load_callback()
        if not isinstance(existing_raw, dict):
            raise ValueError("Plugin configuration load function must return a JSON object")
        existing = {
            field.name: self._validate_value(field, existing_raw[field.name])
            for field in self.fields
            if field.name in existing_raw
        }
        values = dict(submitted)
        for field in self.fields:
            if field.secret and values.get(field.name, "") == "":
                if field.name in existing:
                    values[field.name] = existing[field.name]
                else:
                    values.pop(field.name, None)
        unknown = sorted(set(values) - {field.name for field in self.fields})
        if unknown:
            raise ValueError(f"Unknown plugin configuration fields: {', '.join(unknown)}")
        normalized: dict[str, Any] = {}
        for field in self.fields:
            if field.name in values:
                normalized[field.name] = self._validate_value(field, values[field.name])
            elif field.default is not UNSET:
                normalized[field.name] = self._validate_value(field, field.default)
            elif field.required:
                raise ValueError(f"Required plugin configuration field is missing: {field.name}")
        self.save_callback(normalized)
        return normalized

    def delete(self) -> None:
        self.delete_callback()

    def reset(self, names: list[str]) -> None:
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Plugin field names must be a list of strings")
        known = {field.name for field in self.fields}
        unknown = sorted(set(names) - known)
        if unknown:
            raise ValueError("Unknown plugin configuration fields: " + ", ".join(unknown))
        raw = self.load_callback()
        if not isinstance(raw, dict):
            raise ValueError("Plugin configuration load function must return a JSON object")
        for name in names:
            raw.pop(name, None)
        if raw:
            self.save_callback(raw)
        else:
            self.delete_callback()

    def manifest(self) -> dict[str, Any]:
        raw = self.load_callback()
        if not isinstance(raw, dict):
            raise ValueError("Plugin configuration load function must return a JSON object")
        known = {field.name for field in self.fields}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"Plugin configuration has unknown fields: {', '.join(unknown)}")
        values: dict[str, Any] = {}
        for field in self.fields:
            if field.name in raw:
                values[field.name] = self._validate_value(field, raw[field.name])
            elif field.default is not UNSET:
                values[field.name] = self._validate_value(field, field.default)
        return {
            "format": "json",
            "required": self.required,
            "storage": self.storage,
            "fields": [
                field.manifest(
                    values.get(field.name, None if field.default is UNSET else field.default),
                    field.name in raw and raw[field.name] not in {None, ""},
                )
                for field in self.fields
            ],
        }


@dataclass(frozen=True)
class PluginInitializationContext:
    kind: str
    module_path: Path
    working_directory: Path


@dataclass(frozen=True)
class PluginSpec:
    kind: str
    name: str
    description: str
    origin: str
    factory: Callable[..., Any]
    configuration: PluginConfiguration | None = None
    teardown: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if self.kind not in PLUGIN_KINDS:
            raise ValueError(f"Invalid plugin kind: {self.kind!r}")
        if not self.name.strip() or self.name != self.name.lower():
            raise ValueError(f"Plugin name must be non-empty lowercase text: {self.name!r}")
        if not self.description.strip():
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a description")
        if not callable(self.factory):
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a callable factory")
        if not callable(self.teardown):
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a teardown callback")

    def manifest(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "origin": self.origin,
            "configuration": self.configuration.manifest() if self.configuration else None,
        }


@dataclass(frozen=True)
class PluginFile:
    kind: str
    name: str
    origin: str

    def manifest(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "origin": self.origin,
            "initialized": False,
            "description": "",
            "configuration": None,
        }


class PluginCatalog:
    def __init__(self) -> None:
        self._specs: dict[str, dict[str, PluginSpec]] = {kind: {} for kind in PLUGIN_KINDS}
        self._files: dict[str, dict[str, PluginFile]] = {kind: {} for kind in PLUGIN_KINDS}

    def record_file(self, plugin_file: PluginFile) -> None:
        existing = self._files[plugin_file.kind].get(plugin_file.name)
        if existing:
            raise ValueError(
                f"Duplicate {plugin_file.kind} plugin file {plugin_file.name!r}: "
                f"{existing.origin} and {plugin_file.origin}"
            )
        self._files[plugin_file.kind][plugin_file.name] = plugin_file

    def register(self, spec: PluginSpec) -> None:
        existing = self._specs[spec.kind].get(spec.name)
        if existing:
            raise ValueError(
                f"Duplicate {spec.kind} plugin {spec.name!r}: {existing.origin} and {spec.origin}"
            )
        self._specs[spec.kind][spec.name] = spec

    def get(self, kind: str, name: str) -> PluginSpec:
        try:
            return self._specs[kind][name.lower()]
        except KeyError as error:
            available = ", ".join(self.names(kind)) or "NONE"
            raise ValueError(f"Unknown {kind} plugin {name!r}; discovered: {available}") from error

    def specs(self, kind: str) -> tuple[PluginSpec, ...]:
        return tuple(self._specs[kind][name] for name in self.names(kind))

    def names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self._specs[kind]))

    def files(self, kind: str) -> tuple[PluginFile, ...]:
        return tuple(self._files[kind][name] for name in sorted(self._files[kind]))

    def discovered_names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self._files[kind]))

    def manifests(self) -> dict[str, list[dict[str, Any]]]:
        return {kind: [spec.manifest() for spec in self.specs(kind)] for kind in PLUGIN_KINDS}

    def shutdown(self) -> None:
        errors: list[str] = []
        specs = [spec for kind in PLUGIN_KINDS for spec in self.specs(kind)]
        for spec in reversed(specs):
            try:
                assert spec.teardown is not None
                spec.teardown()
            except Exception as error:
                errors.append(f"{spec.kind}:{spec.name}: {error}")
        self._specs = {kind: {} for kind in PLUGIN_KINDS}
        self._files = {kind: {} for kind in PLUGIN_KINDS}
        if errors:
            raise RuntimeError("Plugin teardown failed: " + "; ".join(errors))


def _module_name(path: Path, index: int) -> str:
    package_root = Path(__file__).resolve().parents[1]
    if package_root in path.parents:
        relative = path.relative_to(package_root.parent).with_suffix("")
        return ".".join(relative.parts)
    return f"prediction_external_plugin_{index}_{path.stem}"


def _load_module(path: Path, index: int) -> ModuleType:
    module_name = _module_name(path, index)
    if module_name.startswith("prediction_market_agent."):
        return importlib.import_module(module_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to import plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_plugin_catalog(
    directory_config: PluginDirectoryConfig,
    *,
    enabled: dict[str, tuple[str, ...]],
    working_directory: Path | None = None,
) -> PluginCatalog:
    catalog = PluginCatalog()
    root = (working_directory or Path.cwd()).resolve()
    index = 0
    for kind in PLUGIN_KINDS:
        for directory in directory_config.directories[kind]:
            if not directory.exists():
                continue
            if not directory.is_dir():
                raise ValueError(f"Plugin system path is not a directory: {directory}")
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                plugin_name = path.stem.lower()
                catalog.record_file(PluginFile(kind, plugin_name, str(path.resolve())))
                if plugin_name not in enabled.get(kind, ()):
                    continue
                index += 1
                module = _load_module(path.resolve(), index)
                initializer = getattr(module, "initialize_plugin", None)
                if not callable(initializer):
                    raise ValueError(
                        f"Scanned {kind} plugin module must export initialize_plugin(context): {path}"
                    )
                context = PluginInitializationContext(
                    kind=kind,
                    module_path=path.resolve(),
                    working_directory=root,
                )
                initialized = initializer(context)
                if not isinstance(initialized, PluginSpec):
                    raise ValueError(f"Plugin initializer must return one PluginSpec: {path}")
                if initialized.kind != kind:
                    raise ValueError(
                        f"Plugin {initialized.name!r} returned kind {initialized.kind!r}, expected {kind!r}"
                    )
                if initialized.name != plugin_name:
                    raise ValueError(
                        f"Plugin name {initialized.name!r} must equal file name {plugin_name!r}: {path}"
                    )
                catalog.register(initialized)
    return catalog


def close_plugin_instances(instances: list[Any]) -> None:
    """Close resources created by a plugin factory, when those objects expose close()."""
    errors: list[str] = []
    while instances:
        instance = instances.pop()
        close = getattr(instance, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                errors.append(str(error))
    if errors:
        raise RuntimeError("; ".join(errors))


def load_plugin_catalog(config: Any) -> PluginCatalog:
    """Load category locations from plugin-directory config, then initialize every discovered plugin."""
    directory_config = PluginDirectoryConfig.load(config.plugin_directories_file)
    managed = ManagedRuntimeConfig.load(config.management_file)
    enabled = {
        "api": managed.selected("api", config.market_api_plugins),
        "decision_provider": managed.selected(
            "decision_provider", config.decision_providers
        ),
        "decision_strategy": (
            managed.decision_strategy or config.decision_strategy_name,
        ),
        "research_tool": managed.selected(
            "research_tool", config.research_tool_plugins
        ),
        "risk": managed.selected("risk", config.risk_plugins),
        "hook": managed.selected("hook", config.hook_plugins),
    }
    return discover_plugin_catalog(
        directory_config,
        enabled=enabled,
        working_directory=getattr(config, "working_directory", Path.cwd()),
    )
