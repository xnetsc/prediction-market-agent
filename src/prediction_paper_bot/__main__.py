from __future__ import annotations

import argparse
import json
import logging
import time

from .decision import make_provider
from .engine import TradingEngine
from .models import Config
from .plugins import load_api_plugins
from .plugins.discovery import load_plugin_catalog
from .plugins.base import platform_state_path
from .reporting import build_report
from .dashboard import serve
from .managed_config import PLUGIN_KINDS


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
        "loaded_env_file": config.loaded_env_file or "NONE",
        "decision_provider_priority": list(config.decision_providers),
        "available_decision_providers": available,
        "unavailable_decision_providers": unavailable,
        "configured_api_plugins": list(config.market_api_plugins),
        "registered_api_plugins": registered_plugins,
        "loaded_api_plugin_capabilities": plugin_status,
        "plugin_sdk_config": str(config.plugin_sdk_config_file),
        "discovered_plugin_files": {
            kind: list(catalog.discovered_names(kind))
            for kind in PLUGIN_KINDS
        },
        "decision_strategy_plugin": strategy_status,
        "plugin_manifests": catalog.manifests(),
        "state_file": str(config.state_file),
        "session_db": str(config.session_db),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-platform prediction-market Agent trading bot"
    )
    parser.add_argument(
        "command", choices=("once", "run", "status", "doctor", "provider-test", "report", "serve")
    )
    parser.add_argument("--verbose", action="store_true")
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
    config = Config.from_env()
    if args.command == "doctor":
        print(json.dumps(_doctor(config), ensure_ascii=False, indent=2))
        return
    if args.command == "serve":
        serve(config)
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
                    "risk_metrics": {},
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
    engine = TradingEngine(config)
    try:
        if args.command == "once":
            print(json.dumps(engine.run_once(), ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(engine.status(), ensure_ascii=False, indent=2))
        else:
            engine.run_forever()
    finally:
        engine.close()


if __name__ == "__main__":
    main()
