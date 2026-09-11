from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.management import PluginManagementService
from prediction_market_agent.plugin_system.managed_config import ManagedRuntimeConfig
from prediction_market_agent.plugin_system.managed_config import PLUGIN_KINDS
from prediction_market_agent.plugin_system.discovery import (
    PluginCatalog,
    PluginConfigField,
    PluginConfiguration,
    PluginFile,
    PluginSpec,
    discover_plugin_catalog,
)
from prediction_market_agent.plugins.api._polymarket.config import PolymarketPluginConfig
from prediction_market_agent.plugins.api._polymarket.write import PolymarketWriteTransport
from prediction_market_agent.core.risk import NetworkWriteGate
from prediction_market_agent.plugin_system.config import PluginDirectoryConfig


class _Model:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self, **kwargs):
        del kwargs
        return dict(self.__dict__)


class _Handle:
    def __init__(self, transaction_id):
        self.transaction_id = transaction_id

    def wait(self):
        return _Model(transactionId=self.transaction_id, state="CONFIRMED")


class _Paginator:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


class _PolymarketClient:
    def __init__(self):
        self.orders = []
        self.environment = SimpleNamespace(collateral_token="0x" + "1" * 40)

    def place_limit_order(self, **values):
        self.orders.append(("limit", values))
        return _Model(ok=True, order_id="limit-1", status="live")

    def place_market_order(self, **values):
        self.orders.append(("market", values))
        return _Model(ok=True, order_id="market-1", status="matched")

    def cancel_orders(self, *, order_ids):
        return _Model(canceled=tuple(order_ids), not_canceled={})

    def list_positions(self, **values):
        self.positions_filter = values
        return _Paginator(
            [_Model(token_id="token-1", condition_id="0x" + "2" * 64)]
        )

    def redeem_positions(self, *, condition_id):
        return _Handle("redeem-" + condition_id[-4:])

    def transfer_erc20(self, **values):
        self.transfer_values = values
        return _Handle("transfer-1")

    def close(self):
        self.closed = True


class PluginSystemTests(unittest.TestCase):
    def test_every_plugin_category_has_an_initializable_complete_example(self) -> None:
        project = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory) / "example-plugin-directories.json"
            categories = {
                "api": [str(project / "examples/api_plugins")],
                "decision_provider": [str(project / "examples/decision_provider_plugins")],
                "decision_strategy": [str(project / "examples/decision_strategy_plugins")],
                "research_tool": [str(project / "examples/research_tool_plugins")],
                "risk": [str(project / "examples/risk_plugins")],
                "agent_policy": [str(project / "examples/agent_policy_plugins")],
            }
            directory_path.write_text(json.dumps({"categories": categories}), encoding="utf-8")
            selected = {
                "api": ("static_demo",),
                "decision_provider": ("static_provider",),
                "decision_strategy": ("example_strategy",),
                "research_tool": ("static_evidence",),
                "risk": ("reject_operation",),
                "agent_policy": ("refuse_tool",),
            }
            catalog = discover_plugin_catalog(
                PluginDirectoryConfig.load(directory_path),
                enabled=selected,
                working_directory=project,
            )
            try:
                runtime = Config()
                self.assertEqual(catalog.get("api", "static_demo").factory(runtime).name, "static_demo")
                self.assertEqual(catalog.get("decision_provider", "static_provider").factory(runtime).name, "static_provider")
                strategy = catalog.get("decision_strategy", "example_strategy").factory(runtime)
                self.assertIn("evidence", strategy.instructions.lower())
                research = catalog.get("research_tool", "static_evidence").factory(runtime)
                self.assertIn("READ_STATIC_EVIDENCE", research.descriptions)
                risk = catalog.get("risk", "reject_operation").factory(runtime, {})
                policy = catalog.get("agent_policy", "refuse_tool").factory(runtime, {})
                # A target the coordinator never dispatches is never consulted, so each example
                # must claim one of the two real target families.
                self.assertEqual(risk.target, "market:*")
                self.assertEqual(policy.target, "agent:actions")
            finally:
                catalog.shutdown()

    def test_every_configuration_field_requires_description(self) -> None:
        with self.assertRaisesRegex(ValueError, "description"):
            PluginConfigField("API_KEY", "API key", "secret", "")

    def test_plugin_owned_json_callbacks_validate_defaults_and_preserve_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            load, save, delete, storage = json_file_callbacks(path)
            config = PluginConfiguration(
                (
                    PluginConfigField("NAME", "Name", "string", "Display name.", default="bot"),
                    PluginConfigField("TOKEN", "Token", "secret", "Private token."),
                ),
                load,
                save,
                delete,
                storage,
            )
            self.assertEqual(config.load(), {"NAME": "bot"})
            config.save({"NAME": "first", "TOKEN": "secret-value"})
            config.save({"NAME": "second", "TOKEN": ""})
            self.assertEqual(load(), {"NAME": "second", "TOKEN": "secret-value"})
            manifest = config.manifest()
            token = next(field for field in manifest["fields"] if field["name"] == "TOKEN")
            self.assertEqual(token["value"], "")
            self.assertTrue(token["configured"])
            config.reset(["TOKEN"])
            self.assertEqual(load(), {"NAME": "second"})
            config.delete()
            self.assertFalse(path.exists())
            self.assertEqual(config.load(), {"NAME": "bot"})

    def test_required_fields_render_before_first_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            load, save, delete, storage = json_file_callbacks(Path(directory) / "new.json")
            config = PluginConfiguration(
                (PluginConfigField("VALUE", "Value", "number", "Required value.", required=True),),
                load,
                save,
                delete,
                storage,
            )
            field = config.manifest()["fields"][0]
            self.assertFalse(field["configured"])
            self.assertIsNone(field["value"])
            with self.assertRaisesRegex(ValueError, "Required"):
                config.load()

    def test_disabled_file_is_discovered_without_import_or_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api_dir = root / "api"
            api_dir.mkdir()
            (api_dir / "danger.py").write_text(
                "raise RuntimeError('module must not be imported while disabled')\n",
                encoding="utf-8",
            )
            directory_path = root / "plugin-directories.json"
            directory_path.write_text(
                json.dumps(
                    {
                        "categories": {
                            "api": [str(api_dir)],
                            "decision_provider": [],
                            "decision_strategy": [],
                            "research_tool": [],
                            "risk": [],
                            "agent_policy": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog = discover_plugin_catalog(
                PluginDirectoryConfig.load(directory_path),
                enabled={kind: () for kind in PLUGIN_KINDS},
            )
            self.assertEqual(catalog.discovered_names("api"), ("danger",))
            self.assertEqual(catalog.names("api"), ())

    def test_catalog_shutdown_calls_teardown_and_unregisters_everything(self) -> None:
        calls = []
        catalog = PluginCatalog()
        catalog.record_file(PluginFile("agent_policy", "sample", "/tmp/sample.py"))
        catalog.register(
            PluginSpec(
                "agent_policy",
                "sample",
                "Lifecycle sample.",
                "/tmp/sample.py",
                lambda config, services: None,
                None,
                lambda: calls.append("teardown"),
            )
        )
        catalog.shutdown()
        self.assertEqual(calls, ["teardown"])
        self.assertEqual(catalog.names("agent_policy"), ())
        self.assertEqual(catalog.discovered_names("agent_policy"), ())

    def test_management_enables_and_disables_with_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(management_file=Path(directory) / "managed.json")
            service = PluginManagementService(config)
            initial = service.manifest()
            disabled = initial["plugins"]["agent_policy"][0]
            self.assertFalse(disabled["initialized"])
            self.assertEqual(disabled["description"], "")
            payload = {
                "enabled": {
                    "api": ["binance", "polymarket"],
                    "decision_provider": ["codex", "claude", "openai_compatible"],
                    "decision_strategy": ["general_agent"],
                    "research_tool": ["standard_research"],
                    "risk": ["portfolio_limits", "custom_rules"],
                    "agent_policy": ["agent_actions"],
                },
                "decision_strategy": "general_agent",
            }
            enabled = service.save_enabled(payload)
            policy = enabled["plugins"]["agent_policy"][0]
            self.assertTrue(policy["initialized"])
            self.assertTrue(policy["configuration"]["fields"])
            payload["enabled"]["agent_policy"] = []
            disabled_again = service.save_enabled(payload)
            self.assertFalse(disabled_again["plugins"]["agent_policy"][0]["initialized"])
            payload["enabled"]["decision_strategy"] = []
            payload["decision_strategy"] = ""
            without_strategy = service.save_enabled(payload)
            self.assertEqual(without_strategy["decision_strategy"], "")
            self.assertFalse(
                any(
                    item["enabled"]
                    for item in without_strategy["plugins"]["decision_strategy"]
                )
            )
            self.assertEqual(
                ManagedRuntimeConfig.load(config.management_file).decision_strategy,
                "",
            )

    def test_manual_refresh_unloads_and_unregisters_removed_plugin_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_dir = root / "agent_policy"
            policy_dir.mkdir()
            marker = root / "torn-down.txt"
            plugin_path = policy_dir / "sample.py"
            plugin_path.write_text(
                "from prediction_market_agent.plugin_system.discovery import PluginSpec\n"
                "def initialize_plugin(context):\n"
                f"    marker = __import__('pathlib').Path({str(marker)!r})\n"
                "    return PluginSpec('agent_policy','sample','sample policy',str(context.module_path),lambda config, services: None,None,lambda: marker.write_text('done', encoding='utf-8'))\n",
                encoding="utf-8",
            )
            directory_path = root / "plugin-directories.json"
            directory_path.write_text(
                json.dumps({"categories": {
                    "api": [], "decision_provider": [], "decision_strategy": [],
                    "research_tool": [], "risk": [], "agent_policy": [str(policy_dir)],
                }}),
                encoding="utf-8",
            )
            management_path = root / "managed.json"
            management_path.write_text(
                json.dumps({"enabled": {
                    "api": [], "decision_provider": [], "decision_strategy": [],
                    "research_tool": [], "risk": [], "agent_policy": ["sample"],
                }, "decision_strategy": ""}),
                encoding="utf-8",
            )
            runtime = Config(
                plugin_directories_file=directory_path,
                management_file=management_path,
                market_api_plugins=(),
                decision_providers=(),
                research_tool_plugins=(),
                risk_plugins=(),
                agent_policy_plugins=("sample",),
            )
            service = PluginManagementService(runtime)
            self.assertEqual(service.catalog.names("agent_policy"), ("sample",))
            plugin_path.unlink()
            refreshed = service.refresh()
            self.assertEqual(marker.read_text(encoding="utf-8"), "done")
            self.assertEqual(refreshed["plugins"]["agent_policy"], [])
            self.assertEqual(service.catalog.names("agent_policy"), ())

    def test_ui_service_installs_new_plugin_disabled_without_importing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_dir = root / "plugins" / "agent_policy"
            directory_path = root / "plugin-directories.json"
            directory_path.write_text(
                json.dumps({"categories": {
                    "api": [], "decision_provider": [], "decision_strategy": [],
                    "research_tool": [], "risk": [], "agent_policy": [str(policy_dir)],
                }}),
                encoding="utf-8",
            )
            config = Config(
                working_directory=root,
                plugin_directories_file=directory_path,
                management_file=root / "management.json",
                market_api_plugins=(), decision_providers=(),
                research_tool_plugins=(), risk_plugins=(),
                decision_strategy_name="",
            )
            service = PluginManagementService(config)
            marker = root / "imported.txt"
            result = service.install_plugin(
                "agent_policy",
                "new_policy",
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported')\n"
                "def initialize_plugin(context):\n    raise RuntimeError('disabled')\n",
                str(policy_dir),
            )
            self.assertTrue(Path(result["installed"]).is_file())
            self.assertFalse(marker.exists())
            installed = next(
                item for item in result["management"]["plugins"]["agent_policy"]
                if item["name"] == "new_policy"
            )
            self.assertFalse(installed["initialized"])
            service.shutdown()

    def test_polymarket_official_client_surface_all_write_workflows(self) -> None:
        settings = PolymarketPluginConfig.from_mapping(
            values={
                "POLYMARKET_GAMMA_URL": "https://gamma-api.polymarket.com",
                "POLYMARKET_CLOB_URL": "https://clob.polymarket.com",
                "POLYMARKET_DATA_URL": "https://data-api.polymarket.com",
                "POLYMARKET_RELAYER_URL": "https://relayer-v2.polymarket.com",
                "POLYMARKET_RPC_URL": "https://polygon.drpc.org",
                "POLYMARKET_CHAIN_ID": "137",
                "POLYMARKET_HTTP_PROXY": "DIRECT",
                "POLYMARKET_NETWORK_RULES_JSON": '{"schemes":["https"],"hosts":["clob.polymarket.com"],"methods":["GET","POST","DELETE"],"paths_by_method":{"GET":["/*"],"POST":["/*"],"DELETE":["/*"]}}',
                "POLYMARKET_PRIVATE_KEY": "unused-by-mock",
                "POLYMARKET_API_KEY": "key",
                "POLYMARKET_API_SECRET": "secret",
                "POLYMARKET_API_PASSPHRASE": "passphrase",
                "POLYMARKET_FUNDER_ADDRESS": "0x" + "3" * 40,
                "POLYMARKET_TRANSFER_RECIPIENT": "0x" + "4" * 40,
            }
        )
        gate = NetworkWriteGate(
            allowed_hosts=frozenset(),
            allowed_schemes=frozenset(),
            allowed_methods=frozenset(),
            allowed_read_paths=frozenset(),
        )
        transport = PolymarketWriteTransport(settings, gate)
        client = _PolymarketClient()
        transport._client = client
        quote = transport.get_quote(
            outcome_id="token-1",
            side="BUY",
            amount="5",
            order_type="LIMIT",
            price_limit="0.5",
            reference_price=0.5,
            fee_bps=0,
        )
        order = transport.place_order(
            quote_id=quote["quoteId"], order_type="LIMIT", price_limit="0.5"
        )
        self.assertEqual(order["orderId"], "limit-1")
        self.assertEqual(order["status"], "OPEN")
        self.assertEqual(order["platformStatus"], "live")
        self.assertEqual(transport.cancel_orders(["limit-1"])["canceled"], ["limit-1"])
        redeemed = transport.redeem(["token-1"])
        self.assertEqual(len(redeemed["transactions"]), 1)
        transferred = transport.transfer("OUTBOUND", "1.25")
        self.assertEqual(transferred["amountBaseUnits"], 1_250_000)
        self.assertEqual(client.transfer_values["amount"], 1_250_000)
        with self.assertRaisesRegex(ValueError, "OUTBOUND"):
            transport.transfer("INBOUND", str(10**18))


if __name__ == "__main__":
    unittest.main()


class CatalogCoverageTests(unittest.TestCase):
    def test_every_declared_kind_can_be_enabled(self) -> None:
        """A kind absent from the loader is discovered but never initialised."""
        import re
        from pathlib import Path

        from prediction_market_agent.plugin_system.managed_config import PLUGIN_KINDS

        source = (
            Path(__file__).resolve().parents[1]
            / "src/prediction_market_agent/plugin_system/discovery.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("    enabled = {") : source.index("    return discover_plugin_catalog(")]
        missing = [kind for kind in PLUGIN_KINDS if f'"{kind}"' not in block]
        self.assertEqual(missing, [], "load_plugin_catalog must map every plugin kind")

    def test_business_risk_and_agent_policy_are_separate_kinds(self) -> None:
        from prediction_market_agent.plugin_system.managed_config import PLUGIN_KINDS

        self.assertIn("risk", PLUGIN_KINDS)
        self.assertIn("agent_policy", PLUGIN_KINDS)

    def test_the_only_filtering_kinds_are_business_risk_and_agent_policy(self) -> None:
        """No third filter category may reappear - hooks were removed for exactly this reason."""
        from pathlib import Path

        from prediction_market_agent.plugin_system.managed_config import PLUGIN_KINDS

        self.assertEqual(
            set(PLUGIN_KINDS),
            {
                "api",
                "decision_provider",
                "decision_strategy",
                "market_discovery",
                "research_tool",
                "agent_policy",
                "risk",
            },
        )
        root = Path(__file__).resolve().parents[1] / "src/prediction_market_agent"
        self.assertFalse((root / "core" / "hooks.py").exists())
        self.assertFalse((root / "plugins" / "hooks").exists())

    def test_the_dashboard_offers_exactly_the_kinds_the_backend_declares(self) -> None:
        """A kind listed in one place and not the other makes the UI lie about what is enforced."""
        import re
        from pathlib import Path

        from prediction_market_agent.plugin_system.managed_config import PLUGIN_KINDS

        runtime = Path(__file__).resolve().parents[1] / "src/prediction_market_agent/runtime"
        shell = (runtime / "dashboard.py").read_text(encoding="utf-8")
        views = (runtime / "static/dashboard-views.js").read_text(encoding="utf-8")

        def listed(source: str, name: str) -> set[str]:
            match = re.search(rf"\b{name}\s*=\s*\[(.*?)\]", source, re.S)
            self.assertIsNotNone(match, f"{name} not found")
            return set(re.findall(r"'([a-z_]+)'", match.group(1)))

        self.assertEqual(listed(shell, "KINDS"), set(PLUGIN_KINDS))
        labels = set(re.findall(r"([a-z_]+):'", re.search(r"const LABELS=\{(.*?)\};", shell, re.S).group(1)))
        self.assertEqual(labels, set(PLUGIN_KINDS), "every kind needs a label")

        centre = listed(views, "PLUGIN_CENTER_KINDS")
        self.assertEqual(
            centre,
            set(PLUGIN_KINDS) - {"decision_provider"},
            "the plugin centre shows every kind except model services, which live under #models",
        )
        help_keys = set(re.findall(r"^    ([a-z_]+): \{title:", views, re.M))
        self.assertEqual(help_keys, set(PLUGIN_KINDS), "every kind needs category help")
