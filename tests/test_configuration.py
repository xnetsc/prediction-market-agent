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

    def test_from_env_automatically_loads_local_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            dotenv = root / ".env"
            management = root / "management.json"
            management.write_text(
                '{"enabled":{"api":["a"],"decision_provider":["p"],'
                '"research_tool":["r"]},"decision_strategy":"s"}',
                encoding="utf-8",
            )
            dotenv.write_text(
                f"BOT_MANAGEMENT_FILE={management}\nAGENT_MAX_TOOL_STEPS=5\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                loaded = Config.from_env()
            finally:
                os.chdir(previous)
            self.assertEqual(loaded.agent_max_tool_steps, 5)
            self.assertEqual(loaded.loaded_env_file, str(dotenv.resolve()))

    def test_process_environment_has_priority_over_dotenv(self) -> None:
        environment = {"AGENT_MAX_TOOL_STEPS": "7"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, environment, clear=True
        ):
            root = Path(directory)
            management = root / "management.json"
            management.write_text(
                '{"enabled":{"api":["a"],"decision_provider":["p"],'
                '"research_tool":["r"]},"decision_strategy":"s"}',
                encoding="utf-8",
            )
            (root / ".env").write_text(
                f"BOT_MANAGEMENT_FILE={management}\nAGENT_MAX_TOOL_STEPS=5\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                loaded = Config.from_env()
            finally:
                os.chdir(previous)
            self.assertEqual(loaded.agent_max_tool_steps, 7)

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

    def test_all_research_tools_may_be_disabled(self) -> None:
        common = Config(
            decision_providers=("provider",),
            market_api_plugins=("api",),
            research_tool_plugins=(),
            decision_strategy_name="strategy",
        )
        common.validate()
