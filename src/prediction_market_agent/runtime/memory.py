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
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS topic_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                market_topic_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                liquidity_usdt REAL NOT NULL DEFAULT 0,
                volume_usdt REAL NOT NULL DEFAULT 0,
                end_time_ms INTEGER,
                reference_price REAL,
                features_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_topic_observation
                ON topic_observations(platform, market_topic_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS discovery_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                selected_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                strategy TEXT NOT NULL,
                market_topic_id TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                priors_json TEXT NOT NULL DEFAULT '[]',
                features_json TEXT NOT NULL DEFAULT '{}',
                reviewed_at INTEGER NOT NULL DEFAULT 0,
                outcome_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_selection
                ON discovery_selections(platform, market_topic_id, selected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_discovery_selection_review
                ON discovery_selections(reviewed_at, selected_at);
            CREATE TABLE IF NOT EXISTS discovery_priors (
                strategy TEXT NOT NULL,
                prior_id TEXT NOT NULL,
                prior_text TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                sample_size INTEGER NOT NULL DEFAULT 0,
                support_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (strategy, prior_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_lessons (
                strategy TEXT NOT NULL,
                lesson_id TEXT NOT NULL,
                lesson_text TEXT NOT NULL,
                bucket TEXT NOT NULL,
                sample_size INTEGER NOT NULL DEFAULT 0,
                support_json TEXT NOT NULL DEFAULT '{}',
                recorded_at INTEGER NOT NULL,
                confirmed_at INTEGER NOT NULL DEFAULT 0,
                retired_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (strategy, lesson_id)
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_lesson_active
                ON discovery_lessons(strategy, retired_at, confirmed_at DESC);
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

    def record_topic_observations(
        self, *, platform: str, observations: list[dict[str, Any]]
    ) -> int:
        """Store this cycle's snapshot of every surveyed topic."""
        if not observations:
            return 0
        now = int(time.time() * 1000)
        rows = [
            (
                now,
                platform,
                str(item["market_topic_id"]),
                str(item.get("title", "")),
                str(item.get("status", "")),
                float(item.get("liquidity_usdt") or 0.0),
                float(item.get("volume_usdt") or 0.0),
                None if item.get("end_time_ms") is None else int(item["end_time_ms"]),
                None if item.get("reference_price") is None else float(item["reference_price"]),
                self._json(item.get("features") or {}),
            )
            for item in observations
        ]
        self.connection.executemany(
            """
            INSERT INTO topic_observations(
                observed_at, platform, market_topic_id, title, status, liquidity_usdt,
                volume_usdt, end_time_ms, reference_price, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def previous_topic_observations(
        self, *, platform: str, before_ms: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """Latest stored snapshot per topic, used to detect what changed since last cycle."""
        cutoff = int(time.time() * 1000) if before_ms is None else int(before_ms)
        rows = self.connection.execute(
            """
            SELECT market_topic_id, observed_at, liquidity_usdt, volume_usdt,
                   end_time_ms, reference_price, status, features_json
            FROM topic_observations
            WHERE platform = ? AND observed_at < ?
            ORDER BY market_topic_id, observed_at DESC
            """,
            (platform, cutoff),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            topic_id = str(row[0])
            if topic_id in latest:
                continue
            latest[topic_id] = {
                "observed_at": int(row[1]),
                "liquidity_usdt": float(row[2]),
                "volume_usdt": float(row[3]),
                "end_time_ms": None if row[4] is None else int(row[4]),
                "reference_price": None if row[5] is None else float(row[5]),
                "status": str(row[6]),
                "features": json.loads(row[7] or "{}"),
            }
        return latest

    def topic_attention_history(self, *, platform: str) -> dict[str, dict[str, Any]]:
        """When each topic was last handed to the decision Agent, and how often."""
        rows = self.connection.execute(
            """
            SELECT market_topic_id, MAX(selected_at), COUNT(*)
            FROM discovery_selections
            WHERE platform = ?
            GROUP BY market_topic_id
            """,
            (platform,),
        ).fetchall()
        return {
            str(row[0]): {"last_selected_at": int(row[1]), "selections": int(row[2])}
            for row in rows
        }

    def record_discovery_selection(
        self,
        *,
        platform: str,
        strategy: str,
        market_topic_id: str,
        position: int,
        reason: str,
        priors: list[str],
        features: dict[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO discovery_selections(
                selected_at, platform, strategy, market_topic_id, position, reason,
                priors_json, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                platform,
                strategy,
                str(market_topic_id),
                int(position),
                reason[:600],
                self._json(list(priors)),
                self._json(features),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def unreviewed_selections(
        self, *, settled_before_ms: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Selections old enough that their downstream decisions have finished."""
        rows = self.connection.execute(
            """
            SELECT id, selected_at, platform, strategy, market_topic_id, priors_json, features_json
            FROM discovery_selections
            WHERE reviewed_at = 0 AND selected_at <= ?
            ORDER BY selected_at ASC
            LIMIT ?
            """,
            (int(settled_before_ms), int(limit)),
        ).fetchall()
        return [
            {
                "id": int(row[0]),
                "selected_at": int(row[1]),
                "platform": str(row[2]),
                "strategy": str(row[3]),
                "market_topic_id": str(row[4]),
                "priors": json.loads(row[5] or "[]"),
                "features": json.loads(row[6] or "{}"),
            }
            for row in rows
        ]

    def selection_downstream(
        self, *, platform: str, market_topic_id: str, since_ms: int, until_ms: int
    ) -> dict[str, Any]:
        """What the decision Agent did with a topic after discovery handed it over."""
        rows = self.connection.execute(
            """
            SELECT status, final_decision_json, proposed_decision_json, execution_json
            FROM decision_ledger
            WHERE platform = ? AND market_topic_id = ?
              AND created_at >= ? AND created_at < ?
            """,
            (platform, str(market_topic_id), int(since_ms), int(until_ms)),
        ).fetchall()
        counts = {
            "decisions": len(rows),
            "acted": 0,
            "held": 0,
            "risk_rejected": 0,
            "execution_error": 0,
            "provider_error": 0,
            "notional": 0.0,
        }
        for status, final_json, proposed_json, execution_json in rows:
            status_text = str(status)
            if status_text == "RISK_REJECTED":
                counts["risk_rejected"] += 1
                continue
            if status_text == "EXECUTION_ERROR":
                counts["execution_error"] += 1
                continue
            if status_text == "PROVIDER_ERROR":
                counts["provider_error"] += 1
                continue
            payload = final_json or proposed_json
            try:
                decision = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                decision = {}
            action = str(decision.get("action", "")).upper()
            if action in {"BUY", "SELL"}:
                counts["acted"] += 1
                counts["notional"] += float(decision.get("notional_usdt") or 0.0)
            elif action:
                counts["held"] += 1
            if execution_json:
                try:
                    execution = json.loads(execution_json)
                except json.JSONDecodeError:
                    execution = {}
                if isinstance(execution, dict) and execution.get("realized_pnl") is not None:
                    counts["realized_pnl"] = float(
                        counts.get("realized_pnl", 0.0)
                    ) + float(execution["realized_pnl"])
        counts["useful"] = counts["acted"] > 0
        return counts

    def mark_selection_reviewed(self, selection_id: int, outcome: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE discovery_selections SET reviewed_at = ?, outcome_json = ? WHERE id = ?",
            (int(time.time() * 1000), self._json(outcome), int(selection_id)),
        )
        self.connection.commit()

    def reviewed_selection_outcomes(
        self, *, strategy: str = "", limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Reviewed selections with the features that earned them the slot."""
        query = """
            SELECT features_json, priors_json, outcome_json, platform
            FROM discovery_selections
            WHERE reviewed_at > 0
        """
        params: list[Any] = []
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        query += " ORDER BY selected_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.connection.execute(query, params).fetchall()
        results = []
        for features_json, priors_json, outcome_json, platform in rows:
            try:
                results.append(
                    {
                        "features": json.loads(features_json or "{}"),
                        "priors": json.loads(priors_json or "[]"),
                        "outcome": json.loads(outcome_json or "{}"),
                        "platform": str(platform),
                    }
                )
            except json.JSONDecodeError:
                continue
        return results

    def settled_decisions(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Decisions whose outcome token was later redeemed, so the truth is known.

        Only platforms that report settlement produce these rows; where a plugin declares
        settlement_status false there is nothing to calibrate against and the list stays empty.
        """
        rows = self.connection.execute(
            """
            SELECT ledger.created_at, ledger.platform, ledger.token_id,
                   ledger.final_decision_json, ledger.proposed_decision_json,
                   ledger.status, redeem.request_json
            FROM decision_ledger AS ledger
            JOIN execution_actions AS redeem
              ON redeem.platform = ledger.platform
             AND redeem.token_id = ledger.token_id
             AND redeem.action = 'REDEEM'
             AND redeem.created_at >= ledger.created_at
            ORDER BY ledger.created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for created_at, platform, token_id, final_json, proposed_json, status, request_json in rows:
            try:
                decision = json.loads(final_json or proposed_json or "{}")
                request = json.loads(request_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(decision, dict) or not isinstance(request, dict):
                continue
            if request.get("winning") is None or decision.get("estimated_probability") is None:
                continue
            results.append(
                {
                    "created_at": int(created_at),
                    "platform": str(platform),
                    "token_id": str(token_id),
                    "status": str(status),
                    "action": str(decision.get("action", "")).upper(),
                    "estimated_probability": float(decision["estimated_probability"]),
                    "confidence": float(decision.get("confidence") or 0.0),
                    "priors": [str(item) for item in decision.get("priors") or []],
                    "won": bool(request["winning"]),
                }
            )
        return results

    def load_discovery_priors(self, strategy: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT prior_id, prior_text, weight, sample_size, support_json, updated_at
            FROM discovery_priors WHERE strategy = ? ORDER BY prior_id
            """,
            (strategy,),
        ).fetchall()
        return [
            {
                "prior_id": str(row[0]),
                "text": str(row[1]),
                "weight": float(row[2]),
                "sample_size": int(row[3]),
                "support": json.loads(row[4] or "{}"),
                "updated_at": int(row[5]),
            }
            for row in rows
        ]

    def save_discovery_prior(
        self,
        *,
        strategy: str,
        prior_id: str,
        text: str,
        weight: float,
        sample_size: int,
        support: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO discovery_priors(
                strategy, prior_id, prior_text, weight, sample_size, support_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy, prior_id) DO UPDATE SET
                prior_text = excluded.prior_text,
                weight = excluded.weight,
                sample_size = excluded.sample_size,
                support_json = excluded.support_json,
                updated_at = excluded.updated_at
            """,
            (
                strategy,
                prior_id,
                text,
                float(weight),
                int(sample_size),
                self._json(support),
                int(time.time() * 1000),
            ),
        )
        self.connection.commit()

    def load_discovery_lessons(self, strategy: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT lesson_id, lesson_text, bucket, sample_size, support_json,
                   recorded_at, confirmed_at
            FROM discovery_lessons
            WHERE strategy = ? AND retired_at = 0
            ORDER BY confirmed_at DESC, recorded_at DESC
            """,
            (strategy,),
        ).fetchall()
        return [
            {
                "lesson_id": str(row[0]),
                "text": str(row[1]),
                "bucket": str(row[2]),
                "sample_size": int(row[3]),
                "support": json.loads(row[4] or "{}"),
                "recorded_at": int(row[5]),
                "confirmed_at": int(row[6]),
            }
            for row in rows
        ]

    def save_discovery_lesson(
        self,
        *,
        strategy: str,
        lesson_id: str,
        text: str,
        bucket: str,
        sample_size: int,
        support: dict[str, Any],
    ) -> None:
        now = int(time.time() * 1000)
        self.connection.execute(
            """
            INSERT INTO discovery_lessons(
                strategy, lesson_id, lesson_text, bucket, sample_size, support_json,
                recorded_at, confirmed_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(strategy, lesson_id) DO UPDATE SET
                lesson_text = excluded.lesson_text,
                bucket = excluded.bucket,
                sample_size = excluded.sample_size,
                support_json = excluded.support_json,
                confirmed_at = excluded.confirmed_at,
                retired_at = 0
            """,
            (strategy, lesson_id, text, bucket, int(sample_size), self._json(support), now, now),
        )
        self.connection.commit()

    def retire_discovery_lesson(self, *, strategy: str, lesson_id: str, reason: str) -> None:
        self.connection.execute(
            """
            UPDATE discovery_lessons
            SET retired_at = ?, support_json = json_patch(support_json, ?)
            WHERE strategy = ? AND lesson_id = ? AND retired_at = 0
            """,
            (
                int(time.time() * 1000),
                self._json({"retired_reason": reason}),
                strategy,
                lesson_id,
            ),
        )
        self.connection.commit()

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
