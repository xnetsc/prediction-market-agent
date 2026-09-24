import time
from ._support import *

class DecisionAndMemoryTests(unittest.TestCase):
    def test_round_history_handoff_names_current_and_previous_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = SessionMemory(Path(directory) / "sessions.sqlite3")
            first_context = {"history_handoff": {"previous_round": None}}
            first = memory.begin_decision(
                platform="venue", market_topic_id="topic", market_id="market",
                token_id="token", strategy_name="s", strategy_sha256="x",
                context=first_context,
            )
            memory.complete_decision(first, status="NO_ACTION")
            previous = memory.latest_decision_reference(
                platform="venue", market_topic_id="topic", token_id="token"
            )
            second_context = {"history_handoff": {"previous_round": previous}}
            second = memory.begin_decision(
                platform="venue", market_topic_id="topic", market_id="market",
                token_id="token", strategy_name="s", strategy_sha256="x",
                context=second_context,
            )
            self.assertEqual(first_context["history_handoff"]["current_round_id"], first)
            self.assertEqual(previous["round_id"], first)
            self.assertEqual(second_context["history_handoff"]["current_round_id"], second)

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
                # The list row says how much research there was; the evidence itself arrives
                # when the row is opened, because it is most of a row's weight.
                self.assertEqual(row["research_count"], 1)
                self.assertEqual(row["risk_decision"]["outcome"], "ALLOW")
                self.assertEqual(row["execution"]["status"], "NO_ACTION")
                full = data.decision(decision_id)
                self.assertEqual(full["research"][0]["tool"], "SEARCH_WEB")
                self.assertEqual(full["risk_decision"]["outcome"], "ALLOW")
                self.assertIn("model_raw_output", full)
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

    def test_collection_activity_survives_without_decision_rows_and_shows_incidents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(
                state_file=root / "state.json",
                session_db=root / "sessions.sqlite3",
                market_api_plugins=("venue",),
            )
            memory = SessionMemory(cfg.session_db)
            memory.record_topic_observations(
                platform="venue",
                observations=[{
                    "market_topic_id": "topic-1",
                    "title": "One market",
                    "status": "OPEN",
                    "liquidity_usdt": 100,
                    "volume_usdt": 200,
                    "features": {"typed_evaluation": {
                        "action": "DEFER", "confidence": 0.92, "provider": "jev"
                    }},
                }],
            )
            memory.record_discovery_selection(
                platform="venue", strategy="built-in", market_topic_id="topic-1",
                position=1, reason="candidate reason", priors=[], features={},
            )
            memory.record_runtime_incident(
                platform="venue", stage="market_selection", severity="error",
                message="schema violation", fallback="prescore",
            )
            memory.close()
            data = AuditData(cfg)
            try:
                activity = data.discovery_activity("venue")
            finally:
                data.management.shutdown()
            self.assertEqual(activity["platforms"][0]["latest_batch_count"], 1)
            self.assertEqual(activity["platforms"][0]["evaluator_counts"], {"DEFER": 1})
            self.assertEqual(activity["recent_selections"][0]["title"], "One market")
            self.assertEqual(activity["incidents"][0]["message"], "schema violation")
            self.assertEqual(activity["incidents"][0]["fallback"], "prescore")

    def test_evaluator_screenings_are_paged_independently_of_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(session_db=root / "sessions.sqlite3")
            memory = SessionMemory(cfg.session_db)
            memory.record_topic_observations(platform="venue", observations=[
                {"market_topic_id": "screened", "title": "Screened market", "status": "OPEN",
                 "liquidity_usdt": 100, "volume_usdt": 200,
                 "features": {"typed_evaluation": {"action": "PRIORITIZE", "quality": 0.8,
                                                     "confidence": 0.91, "provider": "jev",
                                                     "evaluator_name": "jev"}}},
                {"market_topic_id": "unassessed", "title": "Unassessed", "status": "OPEN",
                 "liquidity_usdt": 50, "volume_usdt": 0, "features": {}},
            ])
            memory.record_topic_observations(platform="other", observations=[
                {"market_topic_id": "other", "title": "Other market", "status": "OPEN",
                 "liquidity_usdt": 10, "volume_usdt": 20,
                 "features": {"typed_evaluation": {"action": "DEFER"}}},
            ])
            memory.close()
            data = AuditData(cfg)
            try:
                first = data.evaluator_screenings("venue", limit=1)
                second = data.evaluator_screenings(limit=1, offset=1)
            finally:
                data.management.shutdown()
            self.assertEqual(first["total"], 1)
            self.assertEqual(first["items"][0]["market_topic_id"], "screened")
            self.assertEqual(first["items"][0]["assessment"]["confidence"], 0.91)
            self.assertEqual(second["total"], 2)
            self.assertEqual(len(second["items"]), 1)

    def test_background_screenings_appear_before_older_collection_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(session_db=root / "sessions.sqlite3")
            memory = SessionMemory(cfg.session_db)
            memory.record_topic_observations(platform="venue", observations=[
                {"market_topic_id": "legacy", "title": "Older collection", "status": "OPEN",
                 "features": {"typed_evaluation": {"action": "DEFER"}}},
            ])
            seen_at = int(time.time() * 1000) + 1000
            for index in range(2):
                candidate_id = f"new-{index}"
                memory.queue_market_screening(
                    platform="venue", observed_at=seen_at + index,
                    candidates=[{"candidate_id": candidate_id, "topic_id": "topic",
                                 "market_id": f"market-{index}", "title": f"Fresh {index}",
                                 "status": "OPEN", "liquidity_usdt": 100 + index}],
                )
                memory.complete_market_screening(
                    platform="venue", candidate_id=candidate_id,
                    assessment={"action": "PRIORITIZE", "evaluator_name": "laya"},
                    now_ms=seen_at + index,
                )
            memory.queue_market_screening(
                platform="venue", candidates=[{"candidate_id": "pending", "topic_id": "topic",
                                               "title": "Not evaluated"}],
            )
            memory.close()
            data = AuditData(cfg)
            try:
                first = data.evaluator_screenings("venue", limit=2)
                second = data.evaluator_screenings("venue", limit=2, offset=2)
                other = data.evaluator_screenings("other")
            finally:
                data.management.shutdown()
            self.assertEqual(first["total"], 3)
            self.assertEqual([row["title"] for row in first["items"]], ["Fresh 1", "Fresh 0"])
            self.assertEqual([row["source"] for row in first["items"]], ["queue", "queue"])
            self.assertEqual(first["items"][0]["recorded_at"], seen_at + 1)
            self.assertEqual(first["items"][0]["market_id"], "market-1")
            self.assertEqual(first["items"][0]["assessment"]["evaluator_name"], "laya")
            self.assertEqual([row["title"] for row in second["items"]], ["Older collection"])
            self.assertEqual(second["items"][0]["source"], "observation")
            self.assertEqual(other["total"], 0)
