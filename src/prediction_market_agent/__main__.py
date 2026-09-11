from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path

from .agent.decision import make_provider
from .core.config import ApplicationConfigStore, Config, DEFAULT_APPLICATION_CONFIG
from .plugin_system.registry import load_api_plugins
from .plugin_system.discovery import load_plugin_catalog
from .plugin_system.contracts import platform_state_path
from .runtime.reporting import build_report
from .runtime.dashboard import serve
from .runtime.controller import RobotRuntimeManager
from .plugin_system.managed_config import PLUGIN_KINDS
from .plugin_system.managed_config import ManagedRuntimeConfig, save_managed_config


def _doctor(config: Config) -> dict:
    catalog = load_plugin_catalog(config)
    try:
        provider = make_provider(config, catalog)
        available = list(provider.available_names)
        unavailable = provider.unavailable
    except Exception as error:
        available = []
        unavailable = {"all": str(error)}
    try:
        registry, plugins = load_api_plugins(config, catalog)
        plugin_status = {
            item.name: {
                "capabilities": item.capabilities.to_dict(),
                "configuration": item.configuration_manifest(),
            }
            for item in plugins
        }
        registered_plugins = list(registry.registered_names)
    except Exception as error:
        plugin_status = {"error": str(error)}
        registered_plugins = []
    try:
        strategy_name = config.decision_strategy_name
        strategy = catalog.get("decision_strategy", strategy_name).factory(config)
        strategy_status = {"path": str(strategy.path), "sha256": strategy.sha256}
    except Exception as error:
        strategy_status = {"error": str(error)}
    return {
        "application_config_file": str(config.application_config_file),
        "decision_provider_priority": list(config.decision_providers),
        "available_decision_providers": available,
        "unavailable_decision_providers": unavailable,
        "configured_api_plugins": list(config.market_api_plugins),
        "registered_api_plugins": registered_plugins,
        "loaded_api_plugin_capabilities": plugin_status,
        "plugin_directories_file": str(config.plugin_directories_file),
        "discovered_plugin_files": {
            kind: list(catalog.discovered_names(kind))
            for kind in PLUGIN_KINDS
        },
        "decision_strategy_plugin": strategy_status,
        "plugin_manifests": catalog.manifests(),
        "state_file": str(config.state_file),
        "session_db": str(config.session_db),
        "auth_db": str(config.auth_db),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-platform prediction-market Agent trading bot"
    )
    parser.add_argument(
        "command",
        choices=("init", "once", "run", "status", "doctor", "provider-test", "report", "serve"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_APPLICATION_CONFIG,
        help="Application JSON configuration (default: config/application.json)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--listen-host",
        help="serve-only listen host override, useful for containers",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        help="serve-only listen port override, useful for platform-assigned ports",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=0,
        help="For report: include rows from the last N hours (0 means all)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "init":
        store = ApplicationConfigStore(args.config)
        if store.path.exists():
            raise FileExistsError(f"Application configuration already exists: {store.path}")
        store.save(store.values())
        values = store.values()
        workdir = Path(str(values["working_directory"])).expanduser().resolve()
        management_path = Path(str(values["management_file"])).expanduser()
        if not management_path.is_absolute():
            management_path = workdir / management_path
        management_path = management_path.resolve()
        if not management_path.exists():
            defaults = ManagedRuntimeConfig.load(management_path)
            save_managed_config(management_path, defaults.to_dict())
        print(
            json.dumps(
                {
                    "application_config": str(store.path),
                    "plugin_selection": str(management_path),
                    "next": f"prediction-market-agent --config {store.path} serve",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    config = Config.load(args.config)
    if args.command == "doctor":
        print(json.dumps(_doctor(config), ensure_ascii=False, indent=2))
        return
    if args.command == "serve":
        serve(config, host=args.listen_host, port=args.listen_port)
        return
    if args.command == "report":
        since_ms = (
            int((time.time() - args.since_hours * 3600) * 1000)
            if args.since_hours > 0
            else 0
        )
        multiple = len(config.market_api_plugins) > 1
        state_files = {
            name: platform_state_path(config.state_file, name, multiple)
            for name in config.market_api_plugins
        }
        print(
            json.dumps(
                build_report(config.session_db, state_files, since_ms=since_ms),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "provider-test":
        result = make_provider(config).decide(
            {
                "market": {"title": "Provider connection test", "question": "No real market"},
                "order_book": {"best_bid": 0.49, "best_ask": 0.51},
                "portfolio": {
                    "cash": None,
                    "equity": None,
                    "position": None,
                },
                "risk_manifests": {},
                "instruction": "Return HOLD. This is only a provider connectivity test.",
            }
        )
        print(
            json.dumps(
                {
                    "provider": result.provider,
                    "decision": result.decision.to_dict(),
                    "research_trace": result.research_trace,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    runtime = RobotRuntimeManager(config.application_config_file)
    if args.command == "once":
        print(json.dumps(runtime.execute_once(), ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        try:
            print(
                json.dumps(
                    runtime.reconcile(start_runtimes=False),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            runtime.stop()
        return
    try:
        status = runtime.reconcile()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
