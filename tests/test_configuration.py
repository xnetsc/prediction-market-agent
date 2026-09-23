import json

from ._support import *
from prediction_market_agent.core.config import APPLICATION_FIELDS, ApplicationConfigStore

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
                    "model_selection_mode": "CONFIGURED",
                }}),
                encoding="utf-8",
            )
            loaded = Config.load(application)
            self.assertEqual(loaded.agent_max_tool_steps, 5)
            self.assertEqual(loaded.model_selection_mode, "CONFIGURED")
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
            default = next(
                item.default for item in APPLICATION_FIELDS
                if item.name == "agent_max_tool_steps"
            )
            self.assertEqual(store.values()["agent_max_tool_steps"], default)
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
            "private_key", "codex_cli_path", "claude_cli_path", "openrouter_api_key",
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
        self.assertEqual(
            enabled["api"], ["binance", "polymarket"], "platforms stay discoverable"
        )


class SavingOneSettingKeepsTheRestTests(unittest.TestCase):
    """A save that mentions one setting is not a request to forget every other one."""

    def _store(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ApplicationConfigStore(Path(directory.name) / "application.json")

    def test_changing_the_language_does_not_switch_paper_trading_off(self) -> None:
        """Replacing the file would have turned simulated orders into real ones, silently."""
        store = self._store()
        store.save({"paper_trading": "on", "paper_trading_funds": 5000})
        store.save({"agent_language": "en"})
        values = store.values()
        self.assertEqual(values["paper_trading"], "on")
        self.assertEqual(values["paper_trading_funds"], 5000)
        self.assertEqual(values["agent_language"], "en")

    def test_a_later_save_still_overwrites_the_setting_it_names(self) -> None:
        store = self._store()
        store.save({"agent_language": "en"})
        store.save({"agent_language": "zh"})
        self.assertEqual(store.values()["agent_language"], "zh")

    def test_forgetting_an_override_is_still_what_reset_is_for(self) -> None:
        store = self._store()
        store.save({"paper_trading": "on", "agent_language": "en"})
        store.reset(["paper_trading"])
        values = store.values()
        self.assertEqual(values["paper_trading"], "off")
        self.assertEqual(values["agent_language"], "en")

    def test_retired_browser_poll_interval_is_ignored_and_removed_on_next_save(self) -> None:
        store = self._store()
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "values": {
                        "dashboard_refresh_seconds": 5,
                        "agent_language": "zh",
                        "paper_trading": "on",
                        "paper_trading_funds": 4321,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertNotIn("dashboard_refresh_seconds", store.values())
        self.assertEqual(store.values()["agent_language"], "zh")
        self.assertEqual(store.values()["paper_trading"], "on")
        self.assertEqual(store.values()["paper_trading_funds"], 4321)
        self.assertNotIn(
            "dashboard_refresh_seconds",
            {field["name"] for field in store.manifest()["fields"]},
        )
        store.save({"agent_language": "en"})
        saved = json.loads(store.path.read_text(encoding="utf-8"))["values"]
        self.assertNotIn("dashboard_refresh_seconds", saved)
        self.assertEqual(saved["agent_language"], "en")
        self.assertEqual(saved["paper_trading"], "on")
        self.assertEqual(saved["paper_trading_funds"], 4321)


class TradingStyleIsNotBuriedTests(unittest.TestCase):
    """The two numbers an operator actually tunes do not belong with database paths."""

    def test_the_settings_exist_with_short_and_small_defaults(self) -> None:
        from prediction_market_agent.core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(Path(directory) / "app.json")
        self.assertEqual(config.strategy_horizon_days, 3)
        self.assertEqual(config.strategy_max_trade_usdt, 25.0)

    def test_they_have_their_own_place_on_the_settings_page(self) -> None:
        shell = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        self.assertIn('id="strategySettings"', shell)
        self.assertIn("styleNames=new Set(['strategy_horizon_days','strategy_max_trade_usdt'])", shell)
        panel = shell[shell.index("<h2>交易风格</h2>"):shell.index("统一网络代理")]
        self.assertIn("偏好", panel, "the numbers are preferences, not rules the robot may not cross")
        self.assertIn("必须在理由里说清楚凭什么", panel)
        self.assertIn("业务风控", panel, "a hard limit is a filter plugin's job, and the page should say so")
