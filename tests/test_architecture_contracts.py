from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from prediction_paper_bot.models import Config
from prediction_paper_bot.plugins.discovery import load_plugin_catalog


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "prediction_paper_bot"


class ArchitectureContractTests(unittest.TestCase):
    def test_sdk_files_contain_no_platform_or_policy_configuration(self) -> None:
        sdk_files = (
            PACKAGE / "sdk_config.py",
            PACKAGE / "managed_config.py",
            PACKAGE / "plugin_management.py",
            PACKAGE / "plugins" / "discovery.py",
        )
        combined = "\n".join(path.read_text(encoding="utf-8") for path in sdk_files).casefold()
        forbidden = (
            "api_key",
            "api_secret",
            "private_key",
            "wallet_address",
            "wallet_id",
            "http_proxy",
            "stop_loss",
            "take_profit",
            "loss_limit",
            "max_position",
            "network_rules_json",
            "binance.com",
            "polymarket.com",
            "execution_mode",
        )
        self.assertEqual([name for name in forbidden if name in combined], [])

    def test_runtime_has_no_execution_mode_or_local_write_branch(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in PACKAGE.rglob("*.py")
            if "__pycache__" not in path.parts
        ).casefold()
        forbidden = (
            "execution_mode",
            "local_simulation_only",
            "simulation_only",
            "simulate_write",
            "simulated_write",
            "mock_transport",
            "fake_transport",
        )
        self.assertEqual([name for name in forbidden if name in combined], [])

    def test_generic_config_has_no_platform_strategy_or_risk_policy_fields(self) -> None:
        tree = ast.parse((PACKAGE / "models.py").read_text(encoding="utf-8"))
        config = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Config"
        )
        fields = {
            node.target.id
            for node in config.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        forbidden = {
            "api_key",
            "api_secret",
            "private_key",
            "wallet_address",
            "wallet_id",
            "base_url",
            "http_proxy",
            "execution_mode",
            "starting_capital",
            "stop_loss",
            "take_profit",
            "loss_limit",
            "max_position",
            "network_rules",
            "allowed_tools",
            "strategy_text",
        }
        self.assertEqual(fields & forbidden, set())

    def test_generic_gateway_accepts_only_normalized_order_statuses(self) -> None:
        source = (PACKAGE / "broker.py").read_text(encoding="utf-8")
        self.assertNotIn('"LIVE"', source)
        self.assertIn("unsupported normalized order status", source)
        for name in ("binance_write.py", "polymarket_write.py"):
            plugin_source = (PACKAGE / "plugins" / name).read_text(encoding="utf-8")
            self.assertIn("platformStatus", plugin_source)

    def test_api_credentials_and_wallet_fields_do_not_block_plugin_loading(self) -> None:
        catalog = load_plugin_catalog(Config.from_env())
        try:
            credential_fragments = (
                "API_KEY",
                "API_SECRET",
                "PRIVATE_KEY",
                "PASSPHRASE",
                "WALLET_ADDRESS",
                "WALLET_ID",
                "FUNDER_ADDRESS",
                "TRANSFER_RECIPIENT",
            )
            wrongly_required: list[str] = []
            for spec in catalog.specs("api"):
                if spec.configuration is None:
                    continue
                for field in spec.configuration.fields:
                    if field.required and any(
                        fragment in field.name for fragment in credential_fragments
                    ):
                        wrongly_required.append(f"{spec.name}:{field.name}")
            self.assertEqual(wrongly_required, [])
        finally:
            catalog.shutdown()

    def test_decision_ledger_and_dedicated_ui_remain_present(self) -> None:
        memory = (PACKAGE / "memory.py").read_text(encoding="utf-8")
        dashboard = (PACKAGE / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS decision_ledger", memory)
        self.assertIn("/api/decisions", dashboard)
        self.assertIn("决策账本", dashboard)
        for name in (
            "context_json",
            "research_json",
            "model_raw_output",
            "proposed_decision_json",
            "risk_decision_json",
            "final_decision_json",
            "execution_json",
        ):
            self.assertIn(name, memory)

    def test_test_suite_contains_no_skip_or_expected_failure_decorators(self) -> None:
        forbidden_attributes = {"skip", "skipIf", "skipUnless", "expectedFailure", "xfail"}
        found: list[str] = []
        for path in (ROOT / "tests").glob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                    found.append(f"{path.name}:{node.lineno}:{node.attr}")
                elif isinstance(node, ast.Name) and node.id in forbidden_attributes:
                    found.append(f"{path.name}:{node.lineno}:{node.id}")
        self.assertEqual(found, [])

    def test_every_documented_example_category_has_source(self) -> None:
        examples = ROOT / "examples"
        expected = {
            "api_plugins": "*.py",
            "decision_provider_plugins": "*.py",
            "decision_strategy_plugins": "*.py",
            "research_tool_plugins": "*.py",
            "risk_plugins": "*.py",
            "hooks": "*.py",
        }
        missing = [name for name, pattern in expected.items() if not list((examples / name).glob(pattern))]
        self.assertEqual(missing, [])

    def test_builtin_plugin_configs_have_complete_sanitized_examples(self) -> None:
        examples = ROOT / "examples" / "plugin_configs"
        filenames = {
            "standard_research": "research_standard.json",
            "agent_actions": "risk_agent_actions.json",
            "dynamic_python": "risk_dynamic_python.json",
            "portfolio_limits": "risk_portfolio_limits.json",
            "jsonl_audit": "hook_jsonl_audit.json",
            "general_agent": "strategy_general_agent.json",
            "timezone_latency": "strategy_timezone_latency.json",
        }
        catalog = load_plugin_catalog(Config.from_env())
        try:
            for kind in (
                "api",
                "decision_provider",
                "decision_strategy",
                "research_tool",
                "risk",
                "hook",
            ):
                for spec in catalog.specs(kind):
                    if spec.configuration is None:
                        continue
                    path = examples / filenames.get(spec.name, f"{spec.name}.json")
                    self.assertTrue(path.is_file(), f"missing sanitized config example: {path}")
                    values = json.loads(path.read_text(encoding="utf-8"))
                    expected = {field.name for field in spec.configuration.fields}
                    self.assertEqual(expected - values.keys(), set(), f"incomplete example: {path}")
                    for field in spec.configuration.fields:
                        if field.field_type != "secret":
                            continue
                        value = values[field.name]
                        self.assertRegex(
                            value,
                            rf"^<{field.name}>$",
                            f"secret example must use an explicit placeholder: {path}:{field.name}",
                        )
        finally:
            catalog.shutdown()

    def test_public_release_ignore_rules_cover_private_runtime_material(self) -> None:
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            ".env",
            "config/plugins/*.json",
            "*.key",
            "*.pem",
            "*.backup",
            "*_sessions.sqlite3",
            "*_state*.json",
        ):
            self.assertIn(pattern, rules)


if __name__ == "__main__":
    unittest.main()
