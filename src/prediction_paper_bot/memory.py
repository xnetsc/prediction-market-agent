from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionMemory:
    """Append-only provider turns and actions, plus bounded context recall."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "simulated_actions" in tables and "execution_actions" not in tables:
            self.connection.execute(
                "ALTER TABLE simulated_actions RENAME TO execution_actions"
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                provider TEXT NOT NULL,
                market_topic_id INTEGER NOT NULL,
                token_id TEXT NOT NULL,
                input_json TEXT NOT NULL,
                raw_output TEXT NOT NULL,
                decision_json TEXT,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_turn_market
                ON provider_turns(market_topic_id, token_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS execution_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                market_topic_id INTEGER NOT NULL,
                token_id TEXT NOT NULL,
                action TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_action_market
                ON execution_actions(market_topic_id, token_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS agent_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                provider TEXT NOT NULL,
                market_topic_id INTEGER NOT NULL,
                token_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                input_json TEXT NOT NULL,
                raw_output TEXT NOT NULL,
                control_json TEXT,
                tool_name TEXT NOT NULL DEFAULT '',
                arguments_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_agent_step_market
                ON agent_steps(market_topic_id, token_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS decision_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                strategy_name TEXT NOT NULL,
                strategy_sha256 TEXT NOT NULL,
                market_topic_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                context_json TEXT NOT NULL,
                research_json TEXT NOT NULL DEFAULT '[]',
                model_raw_output TEXT NOT NULL DEFAULT '',
                proposed_decision_json TEXT,
                risk_decision_json TEXT,
                final_decision_json TEXT,
                execution_json TEXT,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_decision_ledger_lookup
                ON decision_ledger(platform, token_id, created_at DESC);
            """
        )
        for table in ("provider_turns", "execution_actions", "agent_steps"):
            columns = {
                row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            if "platform" not in columns:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN platform TEXT NOT NULL DEFAULT 'legacy'"
                )
            if "decision_id" not in columns:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN decision_id INTEGER"
                )
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_turn_platform_market
                ON provider_turns(platform, market_topic_id, token_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_action_platform_market
                ON execution_actions(platform, market_topic_id, token_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_step_platform_market
                ON agent_steps(platform, market_topic_id, token_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_turn_decision ON provider_turns(decision_id);
            CREATE INDEX IF NOT EXISTS idx_action_decision ON execution_actions(decision_id);
            CREATE INDEX IF NOT EXISTS idx_step_decision ON agent_steps(decision_id);
            """
        )
        self.connection.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def record_turn(
        self,
        *,
        provider: str,
        market_topic_id: str | int,
        token_id: str,
        input_payload: dict[str, Any],
        raw_output: str,
        decision: dict[str, Any] | None,
        status: str,
        error: str = "",
        platform: str,
        decision_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO provider_turns(
                created_at, platform, provider, market_topic_id, token_id, input_json,
                raw_output, decision_json, status, error, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                platform,
                provider,
                market_topic_id,
                token_id,
                self._json(input_payload),
                raw_output,
                self._json(decision) if decision is not None else None,
                status,
                error,
                decision_id,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_action(
        self,
        *,
        market_topic_id: str | int,
        token_id: str,
        action: str,
        request: dict[str, Any],
        result: dict[str, Any],
        platform: str,
        decision_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO execution_actions(
                created_at, platform, market_topic_id, token_id, action, request_json, result_json,
                decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                platform,
                market_topic_id,
                token_id,
                action,
                self._json(request),
                self._json(result),
                decision_id,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_agent_step(
        self,
        *,
        provider: str,
        market_topic_id: str | int,
        token_id: str,
        step_index: int,
        input_payload: dict[str, Any],
        raw_output: str,
        control: dict[str, Any] | None,
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        status: str,
        error: str = "",
        platform: str,
        decision_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO agent_steps(
                created_at, platform, provider, market_topic_id, token_id, step_index,
                input_json, raw_output, control_json, tool_name, arguments_json,
                result_json, status, error, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                platform,
                provider,
                market_topic_id,
                token_id,
                step_index,
                self._json(input_payload),
                raw_output,
                self._json(control) if control is not None else None,
                tool_name,
                self._json(arguments or {}),
                self._json(result) if result is not None else None,
                status,
                error,
                decision_id,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def begin_decision(
        self,
        *,
        platform: str,
        market_topic_id: str | int,
        market_id: str | int,
        token_id: str,
        strategy_name: str,
        strategy_sha256: str,
        context: dict[str, Any],
    ) -> int:
        now = int(time.time() * 1000)
        cursor = self.connection.execute(
            """
            INSERT INTO decision_ledger(
                created_at, updated_at, platform, strategy_name, strategy_sha256,
                market_topic_id, market_id, token_id, context_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTED')
            """,
            (
                now,
                now,
                platform,
                strategy_name,
                strategy_sha256,
                str(market_topic_id),
                str(market_id),
                token_id,
                self._json(context),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def complete_decision(
        self,
        decision_id: int,
        *,
        provider: str = "",
        research: list[dict[str, Any]] | None = None,
        model_raw_output: str = "",
        proposed_decision: dict[str, Any] | None = None,
        risk_decision: dict[str, Any] | None = None,
        final_decision: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
        status: str,
        error: str = "",
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE decision_ledger SET
                updated_at = ?, provider = ?, research_json = ?, model_raw_output = ?,
                proposed_decision_json = ?, risk_decision_json = ?, final_decision_json = ?,
                execution_json = ?, status = ?, error = ?
            WHERE id = ?
            """,
            (
                int(time.time() * 1000),
                provider,
                self._json(research or []),
                model_raw_output,
                self._json(proposed_decision) if proposed_decision is not None else None,
                self._json(risk_decision) if risk_decision is not None else None,
                self._json(final_decision) if final_decision is not None else None,
                self._json(execution) if execution is not None else None,
                status,
                error,
                decision_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown decision ledger id: {decision_id}")
        self.connection.commit()

    def recalled_context(
        self,
        *,
        market_topic_id: str | int,
        token_id: str,
        history_limit: int,
        char_budget: int,
        platform: str,
    ) -> dict[str, list[dict[str, Any]]]:
        turns = self.connection.execute(
            """
            SELECT created_at, provider, input_json, decision_json, status, error
            FROM provider_turns
            WHERE platform = ? AND (market_topic_id = ? OR token_id = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (platform, market_topic_id, token_id, max(history_limit * 2, history_limit)),
        ).fetchall()
        actions = self.connection.execute(
            """
            SELECT created_at, action, request_json, result_json
            FROM execution_actions
            WHERE platform = ? AND (market_topic_id = ? OR token_id = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (platform, market_topic_id, token_id, max(history_limit, 1)),
        ).fetchall()

        packed_turns: list[dict[str, Any]] = []
        packed_actions: list[dict[str, Any]] = []
        used = 0
        for created_at, provider, input_json, decision_json, status, error in turns:
            previous_input = json.loads(input_json)
            item = {
                "created_at": created_at,
                "provider": provider,
                "status": status,
                "error": error,
                "market_snapshot": previous_input.get("market"),
                "portfolio_snapshot": previous_input.get("portfolio"),
                "decision": json.loads(decision_json) if decision_json else None,
            }
            encoded = self._json(item)
            if used + len(encoded) > char_budget or len(packed_turns) >= history_limit:
                break
            packed_turns.append(item)
            used += len(encoded)
        for created_at, action, request_json, result_json in actions:
            item = {
                "created_at": created_at,
                "action": action,
                "request": json.loads(request_json),
                "result": json.loads(result_json),
            }
            encoded = self._json(item)
            if used + len(encoded) > char_budget:
                break
            packed_actions.append(item)
            used += len(encoded)
        packed_turns.reverse()
        packed_actions.reverse()
        return {"prior_turns": packed_turns, "prior_actions": packed_actions}

    def stats(self) -> dict[str, int]:
        turns = self.connection.execute("SELECT COUNT(*) FROM provider_turns").fetchone()[0]
        actions = self.connection.execute("SELECT COUNT(*) FROM execution_actions").fetchone()[0]
        steps = self.connection.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0]
        decisions = self.connection.execute("SELECT COUNT(*) FROM decision_ledger").fetchone()[0]
        return {
            "saved_turns": int(turns),
            "saved_actions": int(actions),
            "saved_agent_steps": int(steps),
            "saved_decisions": int(decisions),
        }

    def close(self) -> None:
        self.connection.close()
