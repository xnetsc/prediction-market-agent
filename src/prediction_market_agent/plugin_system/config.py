from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .managed_config import PLUGIN_KINDS, atomic_write_text


DEFAULT_PLUGIN_DIRECTORY_CONFIG = Path(__file__).resolve().parents[1] / "config/plugin_directories.default.json"


@dataclass(frozen=True)
class PluginDirectoryConfig:
    path: Path
    directories: dict[str, tuple[Path, ...]]
    source_path: Path

    @classmethod
    def load(
        cls, configured: Path, *, working_directory: Path | None = None
    ) -> "PluginDirectoryConfig":
        path = configured.expanduser().resolve()
        source_path = path if path.exists() else DEFAULT_PLUGIN_DIRECTORY_CONFIG.resolve()
        try:
            raw = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Plugin directory configuration is invalid JSON: {source_path}: {error}"
            ) from error
        categories = raw.get("categories") if isinstance(raw, dict) else None
        if not isinstance(categories, dict):
            raise ValueError("Plugin directory configuration must contain a categories object")
        package_root = Path(__file__).resolve().parents[1]
        runtime_root = (working_directory or Path.cwd()).expanduser().resolve()
        resolved: dict[str, tuple[Path, ...]] = {}
        for kind in PLUGIN_KINDS:
            values = categories.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"Plugin category {kind} must be a list of directories")
            paths: list[Path] = []
            for value in values:
                expanded = value.replace("${PACKAGE_ROOT}", str(package_root))
                expanded = expanded.replace("${WORKING_DIRECTORY}", str(runtime_root))
                candidate = Path(expanded).expanduser()
                if not candidate.is_absolute():
                    candidate = source_path.parent / candidate
                paths.append(candidate.resolve())
            resolved[kind] = tuple(paths)
        return cls(path=path, directories=resolved, source_path=source_path)

    @classmethod
    def save(
        cls,
        path: Path,
        categories: dict[str, object],
        *,
        working_directory: Path | None = None,
    ) -> "PluginDirectoryConfig":
        if not isinstance(categories, dict):
            raise ValueError("Plugin directories must be an object")
        unknown = sorted(set(categories) - set(PLUGIN_KINDS))
        if unknown:
            raise ValueError("Unknown plugin categories: " + ", ".join(unknown))
        normalized: dict[str, list[str]] = {}
        for kind in PLUGIN_KINDS:
            values = categories.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"Plugin category {kind} must be a list of directories")
            normalized[kind] = [item.strip() for item in values if item.strip()]
        target = path.expanduser().resolve()
        atomic_write_text(
            target,
            json.dumps(
                {"version": 1, "categories": normalized},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return cls.load(target, working_directory=working_directory)

    @classmethod
    def reset(
        cls, path: Path, *, working_directory: Path | None = None
    ) -> "PluginDirectoryConfig":
        target = path.expanduser().resolve()
        if target.exists():
            target.unlink()
        return cls.load(target, working_directory=working_directory)

    def manifest(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "source_path": str(self.source_path),
            "configured": self.path.exists(),
            "categories": {
                kind: [str(path) for path in paths]
                for kind, paths in self.directories.items()
            },
        }
