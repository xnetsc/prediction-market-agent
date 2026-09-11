import json

from ._support import *

class NetworkGateTests(unittest.TestCase):
    def test_get_allowlist(self) -> None:
        gate = configured_read_gate()
        gate.check("GET", "https://api.binance.com/api/v3/time")

    def test_blocks_every_network_write(self) -> None:
        gate = configured_read_gate()
        with self.assertRaises(NetworkGateError):
            gate.check("POST", "https://api.binance.com/api/v3/time")

    def test_blocks_unlisted_path_even_with_get(self) -> None:
        gate = configured_read_gate()
        with self.assertRaises(NetworkGateError):
            gate.check(
                "GET",
                "https://api.binance.com/sapi/v1/w3w/wallet/prediction/trade/place-order-bundle",
            )

    def test_network_policy_is_data_driven(self) -> None:
        gate = NetworkWriteGate(
            allowed_hosts=frozenset({"example.test"}),
            allowed_schemes=frozenset({"https"}),
            allowed_methods=frozenset({"POST"}),
            allowed_read_paths=frozenset({"/configured"}),
            target_name="network:test",
        )
        gate.check("POST", "https://example.test/configured")
        with self.assertRaises(NetworkGateError):
            gate.check("GET", "https://example.test/configured")

    def test_method_specific_glob_paths(self) -> None:
        gate = NetworkWriteGate(
            allowed_hosts=frozenset({"example.test"}),
            allowed_schemes=frozenset({"https"}),
            allowed_methods=frozenset({"GET", "POST"}),
            allowed_read_paths=frozenset(),
            allowed_paths_by_method={
                "GET": frozenset({"/events/*"}),
                "POST": frozenset({"/order"}),
            },
        )
        gate.check("GET", "https://example.test/events/123")
        gate.check("POST", "https://example.test/order")
        with self.assertRaises(NetworkGateError):
            gate.check("POST", "https://example.test/events/123")


class ConfigurationTests(unittest.TestCase):
    def test_platform_credentials_are_plugin_private_and_manifests_are_redacted(self) -> None:
        common = Config()
        self.assertFalse(hasattr(common, "api_key"))
        self.assertFalse(hasattr(common, "polymarket_private_key"))
        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "binance-visible-only-to-plugin",
                "BINANCE_API_SECRET": "binance-secret",
                "POLYMARKET_PRIVATE_KEY": "private-key",
                "POLYMARKET_API_KEY": "l2-key",
                "POLYMARKET_API_SECRET": "l2-secret",
                "POLYMARKET_API_PASSPHRASE": "l2-passphrase",
            },
            clear=False,
        ):
            manifests = [
                BinancePluginConfig.from_mapping({**BINANCE_ENV, **os.environ}).manifest(),
                PolymarketPluginConfig.from_mapping({**POLYMARKET_ENV, **os.environ}).manifest(),
            ]
        encoded = str(manifests)
        self.assertNotIn("binance-secret", encoded)
        self.assertNotIn("private-key", encoded)
        self.assertNotIn("l2-secret", encoded)

    def test_application_json_loads_typed_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = root / "application.json"
            management = root / "management.json"
            management.write_text(
                '{"enabled":{"api":["a"],"decision_provider":["p"],'
                '"research_tool":["r"]},"decision_strategy":"s"}',
                encoding="utf-8",
            )
            application.write_text(
                json.dumps({"version": 1, "values": {
                    "management_file": str(management),
                    "agent_max_tool_steps": 5,
                }}),
                encoding="utf-8",
            )
            loaded = Config.load(application)
            self.assertEqual(loaded.agent_max_tool_steps, 5)
            self.assertEqual(loaded.shared_http_proxy, "HOST")
            self.assertEqual(
                loaded.host_proxy_file,
                (loaded.working_directory / ".deployment" / "host-proxy.json").resolve(),
            )
            self.assertEqual(loaded.application_config_file, application.resolve())

    def test_application_configuration_reset_restores_default(self) -> None:
        from prediction_market_agent.core.config import ApplicationConfigStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ApplicationConfigStore(root / "application.json")
            store.save({"agent_max_tool_steps": 7})
            field = next(
                item for item in store.manifest()["fields"]
                if item["name"] == "agent_max_tool_steps"
            )
            self.assertTrue(field["configured"])
            store.reset(["agent_max_tool_steps"])
            self.assertEqual(store.values()["agent_max_tool_steps"], 4)
            self.assertFalse(store.path.exists())

    def test_financial_limits_are_not_generic_config_fields(self) -> None:
        common = Config()
        self.assertFalse(hasattr(common, "starting_capital"))
        self.assertFalse(hasattr(common, "stop_loss"))
        self.assertFalse(hasattr(common, "platform_allocations_json"))

    def test_platform_provider_and_policy_controls_are_not_generic_config_fields(self) -> None:
        common = Config()
        forbidden = {
            "http_proxy", "execution_mode", "risk_rules_file", "api_key", "api_secret",
            "private_key", "codex_cli_path", "claude_cli_path", "compatible_api_key",
            "agent_allowed_tools", "agent_allowed_trade_actions", "risk_filter_modules",
            "min_liquidity",
        }
        self.assertFalse(forbidden & set(common.__dict__))
        self.assertEqual(common.shared_http_proxy, "HOST")

    def test_all_research_tools_may_be_disabled(self) -> None:
        common = Config(
            decision_providers=("provider",),
            market_api_plugins=("api",),
            research_tool_plugins=(),
            decision_strategy_name="strategy",
        )
        common.validate()


class ShippedDefaultSelectionTests(unittest.TestCase):
    def test_no_risk_plugin_is_enabled_out_of_the_box(self) -> None:
        """A risk list nobody chose looks like protection while enforcing nothing."""
        import json
        from pathlib import Path

        shipped = Path(__file__).resolve().parents[1] / (
            "src/prediction_market_agent/config/plugin_selection.default.json"
        )
        enabled = json.loads(shipped.read_text(encoding="utf-8"))["enabled"]
        self.assertEqual(enabled["risk"], [])
        self.assertEqual(enabled["hook"], [])
        self.assertEqual(
            enabled["api"], ["binance", "polymarket"], "platforms stay discoverable"
        )
