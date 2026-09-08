from ._support import *

class DecisionAndMemoryTests(unittest.TestCase):
    def test_decision_validation(self) -> None:
        decision = Decision.from_mapping(
            {
                "action": "HOLD",
                "order_type": "MARKET",
                "notional_usdt": 0,
                "quantity_fraction": 0,
                "limit_price": None,
                "confidence": 0.7,
                "estimated_probability": 0.5,
                "rationale": "No edge",
            }
        )
        self.assertEqual(decision.action, "HOLD")
        with self.assertRaises(DecisionProviderError):
            Decision.from_mapping(
                {
                    **decision.to_dict(),
                    "action": "BUY",
                    "order_type": "LIMIT",
                    "limit_price": None,
                }
            )

    def test_sqlite_memory_saves_full_turn_and_recalls_bounded_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = SessionMemory(Path(directory) / "sessions.sqlite3")
            payload = {
                "market": {"title": "BTC up?"},
                "portfolio": {"equity": 100},
                "large": "x" * 2000,
            }
            memory.record_turn(
                provider="codex",
                market_topic_id=1,
                token_id="token-up",
                input_payload=payload,
                raw_output="complete raw output",
                decision={"action": "HOLD", "rationale": "wait"},
                status="OK",
                platform="fake",
            )
            memory.record_action(
                market_topic_id=1,
                token_id="token-up",
                action="HOLD",
                request={"action": "HOLD"},
                result={"status": "NO_ACTION"},
                platform="fake",
            )
            recalled = memory.recalled_context(
                market_topic_id=1,
                token_id="token-up",
                history_limit=12,
                char_budget=1000,
                platform="fake",
            )
            memory.record_agent_step(
                provider="codex",
                market_topic_id=1,
                token_id="token-up",
                step_index=0,
                input_payload={"prompt": "research"},
                raw_output='{"next_action":"DECIDE"}',
                control={"next_action": "DECIDE"},
                status="DECIDE",
                platform="fake",
            )
            self.assertEqual(
                memory.stats(),
                {"saved_turns": 1, "saved_actions": 1, "saved_agent_steps": 1, "saved_decisions": 0},
            )
            self.assertEqual(recalled["prior_turns"][0]["decision"]["action"], "HOLD")
            memory.close()

    def test_decision_ledger_links_context_research_risk_execution_and_ui_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(
                state_file=root / "state.json",
                session_db=root / "sessions.sqlite3",
                market_api_plugins=("binance",),
            )
            memory = SessionMemory(cfg.session_db)
            decision_id = memory.begin_decision(
                platform="binance",
                market_topic_id="topic",
                market_id="market",
                token_id="token",
                strategy_name="test_strategy",
                strategy_sha256="abc",
                context={
                    "market": {"title": "Explainable market"},
                    "order_book": {"best_bid": 0.4, "best_ask": 0.6},
                    "portfolio": {"equity": 12},
                },
            )
            memory.record_agent_step(
                provider="codex", market_topic_id="topic", token_id="token",
                step_index=0, input_payload={"prompt": "why"}, raw_output="raw",
                control={"next_action": "DECIDE"}, status="DECIDE",
                platform="binance", decision_id=decision_id,
            )
            memory.record_turn(
                provider="codex", market_topic_id="topic", token_id="token",
                input_payload={"market": {"title": "Explainable market"}},
                raw_output="raw", decision={"action": "HOLD"}, status="OK",
                platform="binance", decision_id=decision_id,
            )
            memory.record_action(
                market_topic_id="topic", token_id="token", action="HOLD",
                request={"action": "HOLD"}, result={"status": "NO_ACTION"},
                platform="binance", decision_id=decision_id,
            )
            memory.complete_decision(
                decision_id, provider="codex",
                research=[{"tool": "SEARCH_WEB", "result": {"source": "example"}}],
                model_raw_output="raw", proposed_decision={"action": "HOLD"},
                risk_decision={"outcome": "ALLOW"}, final_decision={"action": "HOLD"},
                execution={"status": "NO_ACTION"}, status="NO_ACTION",
            )
            memory.close()
            data = AuditData(cfg)
            try:
                row = data.decisions(10, 0, "binance", "codex", "NO_ACTION", "HOLD")["items"][0]
                self.assertEqual(row["id"], decision_id)
                self.assertEqual(row["agent_steps"], 1)
                self.assertEqual(row["research"][0]["tool"], "SEARCH_WEB")
                self.assertEqual(row["risk_decision"]["outcome"], "ALLOW")
                self.assertEqual(row["execution"]["status"], "NO_ACTION")
            finally:
                data.management.shutdown()

    def test_strategy_plugin_owns_discovery_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.md"
            path.write_text("Configured strategy", encoding="utf-8")
            strategy = DecisionStrategyPlugin.load(
                path,
                topic_statuses=("OPEN",),
                market_statuses=("OPEN",),
                outcome_names=("NO",),
                minimum_topic_liquidity=50,
            )
            topics = [
                Topic("1", "a", "a", "", "", "OPEN", 40, 0),
                Topic("2", "b", "b", "", "", "OPEN", 60, 0),
            ]
            self.assertEqual([item.topic_id for item in strategy.select_topics(topics)], ["2"])
            outcomes = (Outcome("yes", "YES"), Outcome("no", "NO"))
            self.assertEqual([item.name for item in strategy.select_outcomes(outcomes)], ["NO"])

    def test_http_audit_data_exposes_full_saved_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "state.json")
            cfg = Config(
                **{
                    **cfg.__dict__,
                    "session_db": root / "sessions.sqlite3",
                    "market_api_plugins": ("binance",),
                }
            )
            memory = SessionMemory(cfg.session_db)
            memory.record_action(
                platform="binance",
                market_topic_id="topic",
                token_id="token",
                action="BUY_FAILED",
                request={"notional": 2},
                result={"status": "EXECUTION_ERROR", "error": "server rejected"},
            )
            memory.close()
            records = AuditData(cfg).records("actions", 10, 0, "binance")
            self.assertEqual(records["items"][0]["action"], "BUY_FAILED")
            self.assertEqual(records["items"][0]["result"]["error"], "server rejected")
