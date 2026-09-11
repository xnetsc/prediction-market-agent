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
from .config_io import resolve_proxy_settings
from .network_diagnostics import DiagnosticNetworkRoute


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
    selection_only: bool = False
    choices_depend_on: tuple[str, ...] = ()

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
            "selection_only": self.selection_only,
            "choices_depend_on": list(self.choices_depend_on),
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
    choices_callback: Callable[[str], list[dict[str, str]]] | None = None
    choice_fields: tuple[str, ...] = ()
    presets: tuple[dict[str, Any], ...] = ()
    context_choices_callback: Callable[[str, dict[str, Any]], list[dict[str, str]]] | None = None
    retired_fields: tuple[str, ...] = ()
    """Field names this plugin used to accept and has since removed.

    An unknown field is normally an error, because a typo silently ignored is a setting the
    operator believes is in force when it is not. A field the plugin itself deleted is different:
    the value is sitting in configs that were valid when written, and refusing to load would take
    the plugin down on upgrade. Naming them here ignores exactly those and nothing else; the next
    save drops them from the file.
    """

    def __post_init__(self) -> None:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("Plugin configuration schema contains duplicate fields")
        if self.choice_fields and not (callable(self.choices_callback) or callable(self.context_choices_callback)):
            raise ValueError("Dynamic choices require a callback")
        for field in self.fields:
            if set(field.choices_depend_on)-set(names):
                raise ValueError("Unknown choice dependency")
            if field.selection_only and field.name not in self.choice_fields:
                raise ValueError("Selection-only field requires dynamic choices")
        if set(self.choice_fields) - set(names):
            raise ValueError("Unknown dynamic choice fields")
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
        raw = {key: value for key, value in raw.items() if key not in self.retired_fields}
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

    def save(self, submitted: dict[str, Any], *, clear_secrets: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(submitted, dict):
            raise ValueError("Plugin configuration values must be an object")
        clear_secrets = [] if clear_secrets is None else clear_secrets
        if not isinstance(clear_secrets, list) or not all(isinstance(name, str) for name in clear_secrets):
            raise ValueError("clear_secrets must be a list of field names")
        if set(clear_secrets) - {field.name for field in self.fields if field.secret}:
            raise ValueError("Only declared secret fields can be cleared")
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
                if field.name in existing and field.name not in clear_secrets:
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
        raw = {key: value for key, value in raw.items() if key not in self.retired_fields}
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
            "presets": list(self.presets),
            "fields": [
                {**field.manifest(
                    values.get(field.name, None if field.default is UNSET else field.default),
                    field.name in raw and raw[field.name] not in {None, ""},
                ), "dynamic_choices": field.name in self.choice_fields}
                for field in self.fields
            ],
        }


@dataclass(frozen=True)
class PluginInitializationContext:
    kind: str
    module_path: Path
    working_directory: Path
    shared_http_proxy: str = "HOST"
    shared_no_proxy: str = "localhost,127.0.0.1,::1"
    host_proxy_file: Path | None = None

    def proxy_settings(
        self,
        value: str,
        *,
        field_name: str = "HTTP_PROXY",
        snapshot_file: Path | None = None,
    ) -> dict[str, str]:
        """Resolve a plugin override against the application's shared proxy."""
        return resolve_proxy_settings(
            value,
            field_name=field_name,
            inherited_value=self.shared_http_proxy,
            inherited_no_proxy=self.shared_no_proxy,
            snapshot_file=snapshot_file or self.host_proxy_file,
        )


@dataclass(frozen=True)
class PluginReadiness:
    """Plugin-owned answer to whether its current private configuration can run."""

    ready: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ready and self.reasons:
            raise ValueError("A ready plugin cannot report blocking reasons")
        if not self.ready and not self.reasons:
            raise ValueError("A blocked plugin must report at least one reason")

    def manifest(self) -> dict[str, Any]:
        return {"ready": self.ready, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class PluginRuntime:
    """Optional plugin-owned background lifecycle; the host only invokes callbacks."""

    start_callback: Callable[[dict[str, Any]], None]
    stop_callback: Callable[[], None]
    status_callback: Callable[[], dict[str, Any]]

    def __post_init__(self) -> None:
        if not all(
            callable(callback)
            for callback in (
                self.start_callback,
                self.stop_callback,
                self.status_callback,
            )
        ):
            raise ValueError("Plugin runtime callbacks must be callable")

    def start(self, services: dict[str, Any]) -> None:
        self.start_callback(services)

    def stop(self) -> None:
        self.stop_callback()

    def status(self) -> dict[str, Any]:
        value = self.status_callback()
        if not isinstance(value, dict):
            raise ValueError("Plugin runtime status callback must return an object")
        return value


@dataclass(frozen=True)
class PluginControls:
    """Optional management panel; protocols and credentials remain plugin-owned."""

    status_callback: Callable[[], dict[str, Any]]
    action_callback: Callable[[str, dict[str, Any]], dict[str, Any]]
    helper_callback: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not callable(self.status_callback) or not callable(self.action_callback):
            raise ValueError("Plugin controls require status and action callbacks")


@dataclass(frozen=True)
class PluginSpec:
    kind: str
    name: str
    description: str
    origin: str
    factory: Callable[..., Any]
    configuration: PluginConfiguration | None = None
    teardown: Callable[[], None] | None = None
    readiness_callback: Callable[[], PluginReadiness] | None = None
    runtime: PluginRuntime | None = None
    controls: PluginControls | None = None
    network_routes_callback: Callable[[], tuple[DiagnosticNetworkRoute, ...]] | None = None

    def __post_init__(self) -> None:
        if self.kind not in PLUGIN_KINDS:
            raise ValueError(f"Invalid plugin kind: {self.kind!r}")
        if not self.name.strip() or self.name != self.name.lower():
            raise ValueError(f"Plugin name must be non-empty lowercase text: {self.name!r}")
        if not self.description.strip():
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a description")
        if not callable(self.factory):
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a callable factory")
        if self.network_routes_callback is not None and not callable(self.network_routes_callback):
            raise ValueError("Plugin network_routes_callback must be callable")
        if not callable(self.teardown):
            raise ValueError(f"Plugin {self.kind}:{self.name} requires a teardown callback")
        if self.readiness_callback is not None and not callable(self.readiness_callback):
            raise ValueError(
                f"Plugin {self.kind}:{self.name} readiness callback must be callable"
            )

    def readiness(self) -> PluginReadiness:
        if self.readiness_callback is not None:
            result = self.readiness_callback()
            if not isinstance(result, PluginReadiness):
                raise ValueError(
                    f"Plugin {self.kind}:{self.name} readiness callback must return "
                    "PluginReadiness"
                )
            return result
        if self.configuration is None:
            return PluginReadiness(True)
        try:
            self.configuration.load()
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))
        return PluginReadiness(True)

    def manifest(self) -> dict[str, Any]:
        try:
            readiness = self.readiness().manifest()
        except Exception as error:
            readiness = PluginReadiness(False, (str(error),)).manifest()
        return {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "origin": self.origin,
            "configuration": self.configuration.manifest() if self.configuration else None,
            "readiness": readiness,
            "has_runtime": self.runtime is not None,
            "has_controls": self.controls is not None,
            "has_network_routes": self.network_routes_callback is not None,
            "runtime": self.runtime.status() if self.runtime else None,
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
                if spec.runtime is not None:
                    spec.runtime.stop()
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
    shared_http_proxy: str = "HOST",
    shared_no_proxy: str = "localhost,127.0.0.1,::1",
    host_proxy_file: Path | None = None,
) -> PluginCatalog:
    catalog = PluginCatalog()
    root = (working_directory or Path.cwd()).resolve()
    if host_proxy_file is not None:
        host_proxy_file = Path(host_proxy_file).expanduser()
        if not host_proxy_file.is_absolute():
            host_proxy_file = root / host_proxy_file
        host_proxy_file = host_proxy_file.resolve()
    index = 0
    for kind in PLUGIN_KINDS:
        for directory in directory_config.directories.get(kind, ()):
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
                    shared_http_proxy=shared_http_proxy,
                    shared_no_proxy=shared_no_proxy,
                    host_proxy_file=host_proxy_file,
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
    directory_config = PluginDirectoryConfig.load(
        config.plugin_directories_file,
        working_directory=getattr(config, "working_directory", Path.cwd()),
    )
    managed = ManagedRuntimeConfig.load(config.management_file)
    enabled = {
        "api": managed.selected("api", config.market_api_plugins),
        "decision_provider": managed.selected(
            "decision_provider", config.decision_providers
        ),
        "decision_strategy": (
            (managed.decision_strategy,) if managed.decision_strategy else ()
        ),
        "market_discovery": managed.selected(
            "market_discovery", getattr(config, "market_discovery_plugins", ())
        ),
        "research_tool": managed.selected(
            "research_tool", config.research_tool_plugins
        ),
        "agent_policy": managed.selected(
            "agent_policy", getattr(config, "agent_policy_plugins", ())
        ),
        "risk": managed.selected("risk", config.risk_plugins),
    }
    return discover_plugin_catalog(
        directory_config,
        enabled=enabled,
        working_directory=getattr(config, "working_directory", Path.cwd()),
        shared_http_proxy=getattr(config, "shared_http_proxy", "HOST"),
        shared_no_proxy=getattr(
            config, "shared_no_proxy", "localhost,127.0.0.1,::1"
        ),
        host_proxy_file=getattr(config, "host_proxy_file", None),
    )
