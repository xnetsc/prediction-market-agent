from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugin_system.discovery import load_plugin_catalog


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "prediction_market_agent"


class ArchitectureContractTests(unittest.TestCase):
    def test_noncommercial_license_is_present_and_packaged(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        package_metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("PolyForm Noncommercial License 1.0.0", license_text)
        self.assertIn("Any noncommercial purpose is a permitted purpose.", license_text)
        self.assertIn("Required Notice: Copyright 2026 xnetsc.", license_text)
        self.assertIn("商业用途不在免费授权范围内", readme)
        self.assertIn("source-available", readme)
        self.assertIn('license = "LicenseRef-PolyForm-Noncommercial-1.0.0"', package_metadata)
        self.assertIn('license-files = ["LICENSE"]', package_metadata)

    def test_plugin_system_files_contain_no_platform_specific_or_policy_configuration(self) -> None:
        plugin_system_files = (
            PACKAGE / "plugin_system" / "config.py",
            PACKAGE / "plugin_system" / "managed_config.py",
            PACKAGE / "plugin_system" / "management.py",
            PACKAGE / "plugin_system" / "discovery.py",
        )
        combined = "\n".join(path.read_text(encoding="utf-8") for path in plugin_system_files).casefold()
        forbidden = (
            "api_key",
            "api_secret",
            "private_key",
            "wallet_address",
            "wallet_id",
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
        self.assertIn("shared_http_proxy", combined)

    def test_package_layout_uses_current_application_boundaries(self) -> None:
        directories = {
            path.name for path in PACKAGE.iterdir()
            if path.is_dir() and not path.name.startswith("__")
        }
        self.assertEqual(
            directories,
            {"agent", "config", "core", "plugins", "plugin_system", "runtime"},
        )

    def test_runtime_has_no_ambiguous_legacy_execution_mode(self) -> None:
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
        tree = ast.parse((PACKAGE / "core" / "config.py").read_text(encoding="utf-8"))
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
            "scan_interval_seconds",
            "error_backoff_seconds",
            "error_backoff_max_seconds",
            "max_topics_per_cycle",
            "max_decisions_per_cycle",
            "topic_page_size",
        }
        self.assertEqual(fields & forbidden, set())

    def test_generic_runtime_and_deployment_templates_do_not_schedule_platform_scans(self) -> None:
        generic_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PACKAGE / "runtime").glob("*.py")
        )
        for name in (
            "scan_interval_seconds",
            "error_backoff_seconds",
            "error_backoff_max_seconds",
            "max_topics_per_cycle",
        ):
            self.assertNotIn(name, generic_sources)
        self.assertNotIn(
            "Type: ScheduleV2",
            (ROOT / "deploy" / "aws" / "template.yaml").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "triggerType: timer",
            (ROOT / "deploy" / "aliyun" / "s.yaml").read_text(encoding="utf-8"),
        )

    def test_generic_gateway_accepts_only_normalized_order_statuses(self) -> None:
        source = (PACKAGE / "runtime" / "broker.py").read_text(encoding="utf-8")
        self.assertNotIn('"LIVE"', source)
        self.assertIn("unsupported normalized order status", source)
        for platform in ("_binance", "_polymarket"):
            plugin_source = (
                PACKAGE / "plugins" / "api" / platform / "write.py"
            ).read_text(encoding="utf-8")
            self.assertIn("platformStatus", plugin_source)

    def test_api_credentials_and_wallet_fields_do_not_block_plugin_loading(self) -> None:
        catalog = load_plugin_catalog(Config.load())
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
        memory = (PACKAGE / "runtime" / "memory.py").read_text(encoding="utf-8")
        dashboard = (PACKAGE / "runtime" / "dashboard.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS decision_ledger", memory)
        self.assertIn("/api/decisions", dashboard)
        self.assertIn("/api/discovery/activity", dashboard)
        self.assertIn("/api/pnl/summary", dashboard)
        self.assertIn("CREATE TABLE IF NOT EXISTS runtime_incidents", memory)
        self.assertIn("CREATE TABLE IF NOT EXISTS pnl_events", memory)
        self.assertIn("决策账本", dashboard)
        self.assertIn('href="#pnl"', dashboard)
        self.assertIn('data-view="pnl"', dashboard)
        self.assertIn('href="#funds"', dashboard)
        self.assertIn('data-view="funds"', dashboard)
        self.assertIn("账户余额、充值与转出", dashboard)
        self.assertNotIn("setInterval(", dashboard)
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
            "decision_evaluator_plugins": "*.py",
            "decision_strategy_plugins": "*.py",
            "market_discovery_plugins": "*.py",
            "research_tool_plugins": "*.py",
            "risk_plugins": "*.py",
            "risk_rules": "*.py",
            "agent_policy_plugins": "*.py",
            "agent_policy_rules": "*.py",
        }
        missing = [name for name, pattern in expected.items() if not list((examples / name).glob(pattern))]
        self.assertEqual(missing, [])

    def test_generic_evaluator_pipeline_does_not_branch_on_builtin_plugin_name(self) -> None:
        sources = (
            PACKAGE / "agent" / "decision_evaluator.py",
            PACKAGE / "runtime" / "bootstrap.py",
            PACKAGE / "runtime" / "evaluation.py",
            PACKAGE / "runtime" / "market_discovery.py",
        )
        combined = "\n".join(path.read_text(encoding="utf-8") for path in sources).casefold()
        self.assertNotIn("jev", combined)

    def test_builtin_plugin_configs_have_complete_sanitized_examples(self) -> None:
        examples = ROOT / "examples" / "plugin_configs"
        filenames = {
            "standard_research": "research_standard.json",
            "agent_actions": "risk_agent_actions.json",
            "custom_rules": "risk_custom_rules.json",
            "refuse_tool": "refuse_tool.json",
            "reject_operation": "reject_operation.json",
            "general_agent": "strategy_general_agent.json",
            "timezone_latency": "strategy_timezone_latency.json",
        }
        catalog = load_plugin_catalog(Config.load())
        try:
            for kind in (
                "api",
                "decision_provider",
                "decision_evaluator",
                "decision_strategy",
                "research_tool",
                "risk",
                "agent_policy",
            ):
                for spec in catalog.specs(kind):
                    if spec.configuration is None:
                        continue
                    path = examples / filenames.get(spec.name, f"{spec.name}.json")
                    self.assertTrue(path.is_file(), f"missing sanitized config example: {path}")
                    values = json.loads(path.read_text(encoding="utf-8"))
                    expected = {field.name for field in spec.configuration.fields}
                    self.assertEqual(expected, set(values), f"schema drift in example: {path}")
                    for field in spec.configuration.fields:
                        if field.field_type != "secret":
                            continue
                        value = values[field.name]
                        if value == "" and not field.required and not field.needed_to_run:
                            continue
                        self.assertRegex(
                            value,
                            rf"^<{field.name}>$",
                            f"secret example must use an explicit placeholder: {path}:{field.name}",
                        )
        finally:
            catalog.shutdown()

    def test_application_example_is_complete_and_matches_shipped_defaults(self) -> None:
        from prediction_market_agent.core.config import APPLICATION_FIELDS

        values = json.loads(
            (ROOT / "examples" / "application.json").read_text(encoding="utf-8")
        )["values"]
        defaults = {field.name: field.default for field in APPLICATION_FIELDS}
        self.assertEqual(set(values), set(defaults))
        self.assertEqual(values, defaults)

    def test_shipped_selection_examples_only_enable_existing_builtin_plugins(self) -> None:
        package_plugins = PACKAGE / "plugins"
        locations = {
            "api": package_plugins / "api",
            "decision_provider": package_plugins / "providers",
            "decision_evaluator": package_plugins / "evaluators",
            "decision_strategy": package_plugins / "strategies",
            "market_discovery": package_plugins / "market_discovery",
            "research_tool": package_plugins / "research",
            "agent_policy": package_plugins / "agent_policy",
            "risk": package_plugins / "risk",
        }
        available = {
            kind: {
                path.stem for path in directory.glob("*.py")
                if not path.name.startswith("_")
            }
            for kind, directory in locations.items()
        }
        for relative in (
            "examples/plugin_selection.json",
            "src/prediction_market_agent/config/plugin_selection.default.json",
        ):
            document = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            for kind, names in document["enabled"].items():
                self.assertEqual(
                    set(names) - available[kind], set(),
                    f"unknown built-in plugin in {relative}:{kind}",
                )
            strategy = document.get("decision_strategy", "")
            self.assertTrue(not strategy or strategy in available["decision_strategy"])

    def test_provider_model_choices_are_dynamic_plugin_fields(self) -> None:
        catalog = load_plugin_catalog(Config.load())
        try:
            expected = {
                "codex": "CODEX_MODEL",
                "claude": "CLAUDE_MODEL",
                "openrouter": "OPENROUTER_MODEL",
            }
            for plugin_name, field_name in expected.items():
                configuration = catalog.get("decision_provider", plugin_name).configuration
                self.assertIsNotNone(configuration)
                fields = {field.name: field for field in configuration.fields}
                self.assertIn(field_name, fields)
                self.assertTrue(fields[field_name].description.strip())
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


class TheLocalDecisionModelIsShippedTests(unittest.TestCase):
    """It needs a GPU, and the container it would run in has none on macOS.

    So the service is carried in the image as a resource rather than started by it, and the console
    hands it over with the one command that runs it. Packaging it at request time from what the
    image holds is what keeps the download and the image from drifting apart.
    """

    def test_the_image_carries_the_service_and_says_where(self) -> None:
        dockerfile = Path("Dockerfile").read_text()
        self.assertIn("COPY deploy/laya-service /opt/laya-service", dockerfile)
        self.assertIn("ENV LAYA_PACKAGE_DIR=/opt/laya-service", dockerfile)

    def test_the_package_has_everything_but_the_weights(self) -> None:
        root = Path("deploy/laya-service")
        for name in ("server.mjs", "page.html", "start.sh", "package.json", "README.md"):
            self.assertTrue((root / name).exists(), name)
        self.assertTrue((root / "vendor" / "webtorch" / "webtorch" / "js" / "webtorch-main.js").exists(),
                        "the SDK travels with it; a package that needs another checkout is not one")
        for name in ("wgpy-main.js", "wgpy-worker.js", "wgpy_webgpu-1.0.0-py3-none-any.whl",
                     "wgpy_webgl-1.0.0-py3-none-any.whl"):
            self.assertTrue((root / "vendor" / "webtorch" / "dist" / name).exists(), name)
        self.assertIn("models/", (root / ".gitignore").read_text(),
                      "800 MB of weights are fetched once on the machine that uses them")

    def test_the_console_serves_it_and_explains_the_gpu(self) -> None:
        source = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        self.assertIn('"/laya-service.zip"', source)
        self.assertIn('"/api/laya/probe"', source)
        panel = source[source.index('id="layaPanel"'):][:2000]
        self.assertIn("Docker 能否使用 GPU 取决于宿主系统和容器配置", panel)
        self.assertIn("不会在容器内启动 Laya", panel)
        self.assertIn('id="layaEndpoint" type="url"', panel)
        self.assertIn("bash laya-service/start.sh", panel)
        self.assertIn("host.docker.internal:8899", panel)

    def test_one_command_and_it_installs_what_it_needs(self) -> None:
        start = Path("deploy/laya-service/start.sh").read_text()
        self.assertIn("npm install", start)
        server = Path("deploy/laya-service/server.mjs").read_text()
        self.assertIn("async function ensureBrowser", server)
        self.assertIn("'install', 'chromium'", server)
        self.assertIn("async function ensureLocalModel", server)

    def test_the_repository_has_a_getting_started_path(self) -> None:
        guide = Path("docs/GETTING_STARTED.md").read_text()
        for step in ("启动", "接一个 AI 模型服务", "接一个交易平台", "入金", "让它跑起来", "本地粗筛模型"):
            self.assertIn(step, guide)
        self.assertIn("docs/GETTING_STARTED.md", Path("README.md").read_text())
