from __future__ import annotations

import json
import sqlite3
import threading
import time

_CANCELLED: set[tuple[str, int]] = set()
_CANCELLED_LOCK = threading.Lock()
"""Decisions deleted while still running, so the code holding them can stop.

Kept at module level rather than on an instance: the console deletes through its own
SessionMemory and the engine runs through another, and a flag only one of them could see
would be a cancellation nobody obeyed."""
from pathlib import Path
from typing import Any


def _instruction_conditions(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SessionMemory:
    """Append-only provider turns and actions, plus bounded context recall."""

    WAL_LIMIT_BYTES = 64 * 1024 * 1024
    """What the write-ahead log is allowed to keep after a checkpoint has drained it."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
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
            CREATE TABLE IF NOT EXISTS pnl_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                account_mode TEXT NOT NULL,
                currency TEXT NOT NULL,
                event_type TEXT NOT NULL,
                market_topic_id TEXT NOT NULL DEFAULT '',
                market_id TEXT NOT NULL DEFAULT '',
                token_id TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                decision_id INTEGER,
                quantity REAL,
                price REAL,
                cash_delta REAL,
                position_quantity_delta REAL,
                cost_basis_delta REAL,
                realized_pnl_delta REAL,
                fee REAL,
                external_flow_delta REAL,
                cash_after REAL,
                equity_after REAL,
                realized_pnl_after REAL,
                evidence_status TEXT NOT NULL DEFAULT 'complete',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_pnl_events_account
                ON pnl_events(platform, account_mode, currency, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_pnl_events_market
                ON pnl_events(platform, token_id, created_at DESC, id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pnl_events_order_type
                ON pnl_events(platform, account_mode, order_id, event_type)
                WHERE order_id != '';
            """
        )
        ledger_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(decision_ledger)")
        }
        if "readable_json" not in ledger_columns:
            # A plain-language restatement of a record, written once and kept. Recomputing it on
            # every view would spend a model call each time somebody opened the same row.
            self.connection.execute("ALTER TABLE decision_ledger ADD COLUMN readable_json TEXT")
        if "discovery_selection_id" not in ledger_columns:
            self.connection.execute(
                "ALTER TABLE decision_ledger ADD COLUMN discovery_selection_id INTEGER"
            )
        instruction_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(operator_instructions)")
        }
        if instruction_columns and "progress" not in instruction_columns:
            # How far along an open instruction is. Written by the same review that decides whether
            # it is finished, so that a session opened mid-way says more than "still open".
            self.connection.execute(
                "ALTER TABLE operator_instructions ADD COLUMN progress TEXT NOT NULL DEFAULT ''"
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
            CREATE TABLE IF NOT EXISTS funding_continuations (
                request_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                market_topic_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                decision_id INTEGER,
                asked_for REAL NOT NULL,
                currency TEXT NOT NULL,
                reason TEXT NOT NULL,
                conclusion_json TEXT NOT NULL,
                resolved_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_funding_open
                ON funding_continuations(resolved_at);

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
            CREATE TABLE IF NOT EXISTS market_screening_queue (
                platform TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                candidate_json TEXT NOT NULL,
                material_key TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                next_due_at INTEGER NOT NULL,
                last_screened_at INTEGER NOT NULL DEFAULT 0,
                assessment_json TEXT NOT NULL DEFAULT '{}',
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                skip_until INTEGER NOT NULL DEFAULT 0,
                skip_material_key TEXT NOT NULL DEFAULT '',
                skip_reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (platform, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_screening_due
                ON market_screening_queue(platform, next_due_at, first_seen_at);
            CREATE TABLE IF NOT EXISTS scheduled_market_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                market_id TEXT NOT NULL DEFAULT '',
                token_id TEXT NOT NULL DEFAULT '',
                due_at INTEGER NOT NULL,
                reason TEXT NOT NULL,
                source_decision_id INTEGER,
                created_at INTEGER NOT NULL,
                completed_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_market_reviews_due
                ON scheduled_market_reviews(platform, completed_at, due_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_market_review_source
                ON scheduled_market_reviews(source_decision_id)
                WHERE source_decision_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_topic_observation_batch
                ON topic_observations(platform, observed_at DESC);
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
            CREATE TABLE IF NOT EXISTS survey_plans (
                platform TEXT PRIMARY KEY,
                queries_json TEXT NOT NULL DEFAULT '[]',
                next_scan_seconds INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                resume_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operator_instructions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                platform TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'reminder',
                headline TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL DEFAULT '',
                conditions_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                resolution TEXT NOT NULL DEFAULT '',
                progress TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_instruction_status
                ON operator_instructions(status, created_at DESC);
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
            CREATE TABLE IF NOT EXISTS provider_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                decision_id INTEGER NOT NULL,
                subject_provider TEXT NOT NULL,
                reviewer_provider TEXT NOT NULL,
                scores_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_provider_review
                ON provider_reviews(subject_provider, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_provider_review_decision
                ON provider_reviews(decision_id);
            CREATE TABLE IF NOT EXISTS decision_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deleted_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decision_deletions_time
                ON decision_deletions(deleted_at DESC);
            CREATE TABLE IF NOT EXISTS runtime_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                stage TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                fallback TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_incidents_time
                ON runtime_incidents(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_runtime_incidents_platform
                ON runtime_incidents(platform, created_at DESC, id DESC);
            """
        )
        selection_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(discovery_selections)")
        }
        if "last_reviewed_at" not in selection_columns:
            self.connection.execute(
                "ALTER TABLE discovery_selections ADD COLUMN last_reviewed_at INTEGER NOT NULL DEFAULT 0"
            )
        if "finalized_at" not in selection_columns:
            self.connection.execute(
                "ALTER TABLE discovery_selections ADD COLUMN finalized_at INTEGER NOT NULL DEFAULT 0"
            )
        survey_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(survey_plans)")
        }
        if "resume_json" not in survey_columns:
            self.connection.execute(
                "ALTER TABLE survey_plans ADD COLUMN resume_json TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def open_funding_continuation(
        self,
        *,
        request_id: str,
        platform: str,
        market_topic_id: str,
        token_id: str,
        decision_id: int | None,
        asked_for: float,
        currency: str,
        reason: str,
        conclusion: dict[str, Any],
    ) -> None:
        """Remember why money was wanted, so the answer can be read against it later.

        A funding answer that arrives on its own is worthless: "the money is here" says nothing
        about whether the trade that needed it is still worth making. What has to survive the wait
        is the reasoning, the market it was about, and the price it was judged against.
        """
        self.connection.execute(
            """
            INSERT OR REPLACE INTO funding_continuations
            (request_id, created_at, platform, market_topic_id, token_id, decision_id,
             asked_for, currency, reason, conclusion_json, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                request_id, int(time.time() * 1000), platform, str(market_topic_id),
                str(token_id), decision_id, float(asked_for), currency, reason,
                self._json(conclusion),
            ),
        )
        self.connection.commit()

    def open_funding_continuations(self, platform: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT request_id, created_at, market_topic_id, token_id, decision_id,
                   asked_for, currency, reason, conclusion_json
            FROM funding_continuations
            WHERE platform = ? AND resolved_at IS NULL
            ORDER BY created_at
            """,
            (platform,),
        ).fetchall()
        return [
            {
                "request_id": row[0], "asked_at": row[1], "market_topic_id": row[2],
                "token_id": row[3], "decision_id": row[4], "asked_for": row[5],
                "currency": row[6], "reason": row[7],
                "conclusion": json.loads(row[8]) if row[8] else {},
            }
            for row in rows
        ]

    def close_funding_continuation(self, request_id: str) -> None:
        self.connection.execute(
            "UPDATE funding_continuations SET resolved_at = ? WHERE request_id = ?",
            (int(time.time() * 1000), request_id),
        )
        self.connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """One connection per thread, all onto the same file.

        A single shared connection refuses every call from a thread other than the one that made
        it, and this runs in several: each platform's loop, and the console serving pages. The
        refusal was not loud - the cycle logged it and carried on with "stored measurements" - so
        the robot reported itself as running while every review and every decision died on the way
        in. WAL lets the readers proceed alongside the writer, and the busy timeout absorbs the
        moment two of them want to write at once.
        """
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        # Without this the write-ahead log keeps whatever size its largest transaction needed, for
        # good. Deleting a whole ledger touches nearly every page in the database and copies each
        # one into that log: one such delete here left a 290 MB log beside a 311 MB database, and
        # nothing shrinks it back on its own. Checkpoints now return the space.
        connection.execute(f"PRAGMA journal_size_limit={self.WAL_LIMIT_BYTES}")
        self._local.connection = connection
        return connection

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

    def record_pnl_event(self, event: dict[str, Any]) -> int:
        """Append one immutable accounting fact after a platform accepted an action.

        A transfer is an external flow, not profit.  A fill or redemption may change realised
        profit.  Keeping those as separate columns lets the reader reconcile cash without ever
        calling a deposit a win.  Unknown basis is stored as SQL NULL rather than zero.
        """
        required = ("platform", "account_mode", "currency", "event_type")
        missing = [name for name in required if not str(event.get(name, "")).strip()]
        if missing:
            raise ValueError(f"P&L event is missing: {', '.join(missing)}")
        created_at = int(event.get("created_at") or time.time() * 1000)
        fields = (
            "quantity", "price", "cash_delta", "position_quantity_delta",
            "cost_basis_delta", "realized_pnl_delta", "fee", "external_flow_delta",
            "cash_after", "equity_after", "realized_pnl_after",
        )
        numbers = [None if event.get(name) is None else float(event[name]) for name in fields]
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO pnl_events(
                    created_at, platform, account_mode, currency, event_type,
                    market_topic_id, market_id, token_id, order_id, decision_id,
                    quantity, price, cash_delta, position_quantity_delta, cost_basis_delta,
                    realized_pnl_delta, fee, external_flow_delta, cash_after, equity_after,
                    realized_pnl_after, evidence_status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    str(event["platform"]),
                    str(event["account_mode"]).lower(),
                    str(event["currency"]).upper(),
                    str(event["event_type"]).upper(),
                    str(event.get("market_topic_id") or ""),
                    str(event.get("market_id") or ""),
                    str(event.get("token_id") or ""),
                    str(event.get("order_id") or ""),
                    None if event.get("decision_id") is None else int(event["decision_id"]),
                    *numbers,
                    str(event.get("evidence_status") or "complete"),
                    self._json(event.get("metadata") or {}),
                ),
            )
        except sqlite3.IntegrityError as error:
            # An order response may be retried after the venue has already accepted it.  The
            # unique order/type key makes that replay idempotent without hiding other DB errors.
            order_id = str(event.get("order_id") or "")
            if not order_id:
                raise
            row = self.connection.execute(
                """
                SELECT id FROM pnl_events
                WHERE platform = ? AND account_mode = ? AND order_id = ? AND event_type = ?
                """,
                (
                    str(event["platform"]), str(event["account_mode"]).lower(), order_id,
                    str(event["event_type"]).upper(),
                ),
            ).fetchone()
            if row is None:
                raise error
            return int(row[0])
        self.connection.commit()
        return int(cursor.lastrowid)

    def has_pnl_events(self, *, platform: str, account_mode: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM pnl_events WHERE platform = ? AND account_mode = ? LIMIT 1",
            (platform, account_mode.lower()),
        ).fetchone()
        return row is not None

    def record_decision_deletion(
        self, *, source: str, scope: dict[str, Any], result: dict[str, Any]
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO decision_deletions(deleted_at, source, scope_json, result_json)
            VALUES (?, ?, ?, ?)
            """,
            (int(time.time() * 1000), source, self._json(scope), self._json(result)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_runtime_incident(
        self,
        *,
        platform: str,
        stage: str,
        severity: str,
        message: str,
        fallback: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        """Persist an operational failure even when the workflow safely falls back.

        Discovery has several deliberate fallback paths.  A log line alone makes those paths
        invisible in the console, while putting them in the decision ledger incorrectly implies
        that a market decision was completed.  Incidents are therefore their own append-only
        evidence stream.
        """
        cursor = self.connection.execute(
            """
            INSERT INTO runtime_incidents(
                created_at, platform, stage, severity, message, fallback, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                str(platform),
                str(stage),
                str(severity),
                str(message),
                str(fallback),
                self._json(details or {}),
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
        discovery_selection_id: int | None = None,
    ) -> int:
        now = int(time.time() * 1000)
        cursor = self.connection.execute(
            """
            INSERT INTO decision_ledger(
                created_at, updated_at, platform, strategy_name, strategy_sha256,
                market_topic_id, market_id, token_id, context_json, discovery_selection_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTED')
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
                discovery_selection_id,
            ),
        )
        self.connection.commit()
        decision_id = int(cursor.lastrowid)
        handoff = context.setdefault("history_handoff", {})
        if isinstance(handoff, dict):
            handoff["current_round_id"] = decision_id
            self.connection.execute(
                "UPDATE decision_ledger SET context_json = ? WHERE id = ?",
                (self._json(context), decision_id),
            )
            self.connection.commit()
        # Anything registered under this id belonged to an older row: a database recreated since,
        # or a deleted row whose id was reused. It must not stop the decision just opened, and
        # clearing it here is also what keeps the register from growing without end.
        with _CANCELLED_LOCK:
            _CANCELLED.discard(self._cancel_key(decision_id))
        return decision_id

    def latest_decision_reference(
        self, *, platform: str, market_topic_id: str | int, token_id: str
    ) -> dict[str, Any] | None:
        """Identify the round a new CLI session should consider continuing.

        The CLI owns retrieval and context selection.  The framework only supplies the exact
        hand-off pointer that cannot be inferred reliably from a database path alone.
        """
        row = self.connection.execute(
            """
            SELECT id, created_at, provider, status
            FROM decision_ledger
            WHERE platform = ? AND (market_topic_id = ? OR token_id = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (platform, str(market_topic_id), str(token_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "round_id": int(row[0]),
            "created_at": int(row[1]),
            "provider": str(row[2] or ""),
            "status": str(row[3] or ""),
        }

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
        now = int(time.time() * 1000)
        cursor = self.connection.execute(
            """
            UPDATE decision_ledger SET
                updated_at = ?, provider = ?, research_json = ?, model_raw_output = ?,
                proposed_decision_json = ?, risk_decision_json = ?, final_decision_json = ?,
                execution_json = ?, status = ?, error = ?
            WHERE id = ?
            """,
            (
                now,
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
        revisit_after = int((final_decision or {}).get("revisit_after_seconds") or 0)
        if revisit_after > 0 and status not in self.FAILED_STATUSES:
            identity = self.connection.execute(
                """SELECT platform, market_topic_id, market_id, token_id, strategy_name
                   FROM decision_ledger WHERE id = ?""",
                (int(decision_id),),
            ).fetchone()
            if identity and identity[1] and "discovery" not in str(identity[4]):
                self.connection.execute(
                    """INSERT OR IGNORE INTO scheduled_market_reviews (
                           platform, topic_id, market_id, token_id, due_at, reason,
                           source_decision_id, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(identity[0]), str(identity[1]), str(identity[2]), str(identity[3]),
                     now + min(revisit_after, 2592000) * 1000,
                     str((final_decision or {}).get("revisit_when") or "")[:200],
                     int(decision_id), now),
                )
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

    def topic_verdict_history(self, *, platform: str) -> dict[str, dict[str, Any]]:
        """What was decided about each topic last time, and how often it was left alone.

        Selection used to know only when a topic was last picked and how many times. That cannot
        distinguish "looked at and found fairly priced" from "never examined", so the rounds kept
        spending their few slots re-deciding the same markets and reaching the same HOLD. The
        verdict, its one-line reason and the trigger the round named are what make that answer
        reusable instead of repeatable.
        """
        rows = self.connection.execute(
            """
            SELECT market_topic_id, created_at, final_decision_json
            FROM decision_ledger
            WHERE platform = ? AND strategy_name NOT LIKE '%discovery%'
              AND final_decision_json IS NOT NULL AND market_topic_id != ''
            ORDER BY created_at
            """,
            (str(platform),),
        ).fetchall()
        history: dict[str, dict[str, Any]] = {}
        for topic_id, created_at, final_json in rows:
            try:
                final = json.loads(final_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(final, dict):
                continue
            action = str(final.get("action") or "").upper()
            entry = history.setdefault(
                str(topic_id), {"decisions": 0, "holds": 0, "last_action": "", "last_at": 0,
                                "last_headline": "", "revisit_when": ""}
            )
            entry["decisions"] += 1
            if action == "HOLD":
                entry["holds"] += 1
            entry["last_action"] = action
            entry["last_at"] = int(created_at)
            entry["last_headline"] = str(final.get("headline") or "")[:120]
            entry["revisit_when"] = str(final.get("revisit_when") or "")[:200]
        return history

    SCREENING_HISTORY_LIMIT = 6
    """How many past screenings and past decisions per topic are carried forward."""

    def screening_history(self, *, platform: str) -> dict[str, dict[str, Any]]:
        """Everything already known about each topic: how it was screened, and what came of it.

        The coarse screener judges hundreds of candidates a round and, given only the candidate,
        judges each one as if for the first time - so a market screened and decided a dozen times
        is screened again on the same facts and comes back with the same answer, and the expensive
        round behind it spends its slots re-deciding what is already settled. This is the record it
        was missing: its own past calls on this topic, and the decisions that followed them.
        """
        history: dict[str, dict[str, Any]] = {}

        def entry(topic_id: str) -> dict[str, Any]:
            return history.setdefault(str(topic_id), {
                "screened": [], "screen_counts": {}, "decided": [], "decision_counts": {},
            })

        for topic_id, observed_at, features_json in self.connection.execute(
            """
            SELECT market_topic_id, observed_at, features_json FROM topic_observations
            WHERE platform = ? AND features_json LIKE '%typed_evaluation%'
            ORDER BY observed_at
            """,
            (str(platform),),
        ):
            try:
                typed = (json.loads(features_json or "{}") or {}).get("typed_evaluation")
            except json.JSONDecodeError:
                continue
            if not isinstance(typed, dict) or not typed.get("action"):
                continue
            item = entry(topic_id)
            action = str(typed["action"]).upper()
            item["screen_counts"][action] = item["screen_counts"].get(action, 0) + 1
            item["screened"].append({
                "action": action,
                "confidence": typed.get("confidence"),
                "at_ms": int(observed_at),
            })
            item["screened"] = item["screened"][-self.SCREENING_HISTORY_LIMIT:]

        for topic_id, created_at, final_json in self.connection.execute(
            """
            SELECT market_topic_id, created_at, final_decision_json FROM decision_ledger
            WHERE platform = ? AND strategy_name NOT LIKE '%discovery%'
              AND final_decision_json IS NOT NULL AND market_topic_id != ''
            ORDER BY created_at
            """,
            (str(platform),),
        ):
            try:
                final = json.loads(final_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(final, dict) or not final.get("action"):
                continue
            item = entry(topic_id)
            action = str(final["action"]).upper()
            item["decision_counts"][action] = item["decision_counts"].get(action, 0) + 1
            item["decided"].append({
                "action": action,
                "said": str(final.get("headline") or "")[:120],
                "revisit_when": str(final.get("revisit_when") or "")[:200],
                "at_ms": int(created_at),
            })
            item["decided"] = item["decided"][-self.SCREENING_HISTORY_LIMIT:]
        return history

    def screening_calibration(self, *, platform: str) -> dict[str, Any]:
        """What each screening verdict has been worth on this platform, counted from what followed.

        Per-topic history tells the screener about markets it has seen. This tells it about itself:
        of everything it called worth attention, how much the deciding model then traded, and how
        much it left alone. Pair each topic's latest screen with only its first subsequent full
        decision; earlier or repeated decisions must not be attributed to that screen.
        """
        history = self.screening_history(platform=platform)
        outcomes: dict[str, dict[str, int]] = {}
        for item in history.values():
            if not item["screened"] or not item["decided"]:
                continue
            latest_screen = item["screened"][-1]
            following = next(
                (decision for decision in item["decided"]
                 if decision["at_ms"] >= latest_screen["at_ms"]),
                None,
            )
            if following is None:
                continue
            bucket = outcomes.setdefault(latest_screen["action"], {})
            bucket[following["action"]] = bucket.get(following["action"], 0) + 1
        return {
            "meaning": "每个标的最近一次粗筛之后，首个完整决策做了什么（每标的最多一次）",
            "by_screening_action": outcomes,
            "topics_with_history": len(history),
        }

    def evaluator_quality(self, *, platform: str = "") -> dict[str, float]:
        """Conservatively rank screeners by later full-decision agreement.

        This measures whether a paid full decision found a trade after a screener asked for
        attention, or held after it deferred. It is a cost-usefulness proxy, not trade P&L.
        Cases without a subsequent full decision cannot establish correctness and are excluded.
        """
        rows = self.connection.execute(
            """
            SELECT d.final_decision_json,
                   (SELECT o.features_json FROM topic_observations AS o
                    WHERE o.platform = d.platform
                      AND o.market_topic_id = d.market_topic_id
                      AND o.observed_at <= d.created_at
                      AND o.features_json LIKE '%typed_evaluation%'
                    ORDER BY o.observed_at DESC, o.id DESC LIMIT 1)
            FROM decision_ledger AS d
            WHERE (? = '' OR d.platform = ?)
              AND d.market_topic_id != ''
              AND d.strategy_name NOT LIKE '%discovery%'
              AND d.final_decision_json IS NOT NULL
            ORDER BY d.id DESC LIMIT 1000
            """,
            (platform, platform),
        )
        counts: dict[str, dict[str, int]] = {}
        for final_json, features_json in rows:
            if not features_json:
                continue
            try:
                decision = json.loads(final_json or "{}")
                typed = (json.loads(features_json or "{}") or {}).get("typed_evaluation")
            except (TypeError, ValueError):
                continue
            if not isinstance(decision, dict) or not isinstance(typed, dict):
                continue
            action = str(decision.get("action") or "").upper()
            screened = str(typed.get("action") or "").upper()
            if action not in {"BUY", "SELL", "HOLD"} or screened not in {
                "PRIORITIZE", "NEEDS_DATA", "DEFER", "REJECT"
            }:
                continue
            name = str(typed.get("evaluator_name") or "").strip()
            if not name:
                provider = str(typed.get("provider") or "")
                name = "laya" if provider.startswith("laya:") else (
                    "jev" if provider.startswith(("openrouter:", "custom:")) else ""
                )
            if not name:
                continue
            entry = counts.setdefault(name, {"positive": 0, "positive_right": 0,
                                             "negative": 0, "negative_right": 0})
            observed = "positive" if action in {"BUY", "SELL"} else "negative"
            entry[observed] += 1
            predicted_positive = screened in {"PRIORITIZE", "NEEDS_DATA"}
            if predicted_positive == (observed == "positive"):
                entry[observed + "_right"] += 1
        scores: dict[str, float] = {}
        for name, entry in counts.items():
            total = entry["positive"] + entry["negative"]
            balanced = sum(
                entry[kind + "_right"] / entry[kind] if entry[kind] else 0.5
                for kind in ("positive", "negative")
            ) / 2.0
            scores[name] = round(1.0 + (balanced - 0.5) * total / (total + 20), 4)
        return scores

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
            WHERE finalized_at = 0 AND selected_at <= ? AND last_reviewed_at <= ?
            ORDER BY selected_at ASC
            LIMIT ?
            """,
            (int(settled_before_ms), int(settled_before_ms), int(limit)),
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
        self, *, platform: str, market_topic_id: str, since_ms: int, until_ms: int,
        selection_id: int | None = None,
    ) -> dict[str, Any]:
        """What the decision Agent did with a topic after discovery handed it over."""
        if selection_id is not None:
            rows = self.connection.execute(
                """
                SELECT status, final_decision_json, proposed_decision_json, execution_json
                FROM decision_ledger WHERE discovery_selection_id = ?
                """,
                (int(selection_id),),
            ).fetchall()
        else:
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
            "submitted_orders": 0,
            "accepted_orders": 0,
            "filled_orders": 0,
            "buy_fills": 0,
            "sell_fills": 0,
            "resolved_trades": 0,
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
                counts["submitted_orders"] += 1
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
                if isinstance(execution, dict):
                    status = str(execution.get("status", "")).upper()
                    if status in {"OPEN", "ACCEPTED", "FILLED", "MATCHED", "PARTIALLY_FILLED"}:
                        counts["accepted_orders"] += 1
                    if status in {"FILLED", "MATCHED", "PARTIALLY_FILLED"}:
                        counts["filled_orders"] += 1
                        counts["acted"] += 1
                        counts["notional"] += float(decision.get("notional_usdt") or 0.0)
                        if action == "BUY":
                            counts["buy_fills"] += 1
                        elif action == "SELL":
                            counts["sell_fills"] += 1
        action_rows = self.connection.execute(
            """
            SELECT action, result_json FROM execution_actions
            WHERE platform = ? AND market_topic_id = ? AND created_at >= ? AND created_at < ?
            """,
            (platform, str(market_topic_id), int(since_ms), int(until_ms)),
        ).fetchall()
        for action, result_json in action_rows:
            if str(action).upper() != "REDEEM":
                continue
            try:
                result = json.loads(result_json or "{}")
            except json.JSONDecodeError:
                result = {}
            counts["resolved_trades"] += 1
            if isinstance(result, dict) and result.get("realized_pnl") is not None:
                counts["realized_pnl"] = float(counts.get("realized_pnl", 0.0)) + float(result["realized_pnl"])
        counts["useful"] = counts["filled_orders"] > 0
        counts["outcome_complete"] = counts["resolved_trades"] > 0
        counts["net_pnl_known"] = counts["outcome_complete"] and "realized_pnl" in counts
        return counts

    def mark_selection_reviewed(
        self, selection_id: int, outcome: dict[str, Any], *, final: bool = False
    ) -> None:
        now = int(time.time() * 1000)
        self.connection.execute(
            """UPDATE discovery_selections
               SET reviewed_at = ?, last_reviewed_at = ?, finalized_at = ?, outcome_json = ?
               WHERE id = ?""",
            (now, now, now if final else 0, self._json(outcome), int(selection_id)),
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

    FAILED_STATUSES = frozenset({"PROVIDER_ERROR", "EXECUTION_ERROR", "ERROR", "FAILED"})
    """Statuses where the round did not reach a conclusion because something broke.

    Kept apart from the rest because they answer a different question. A reader looking for what the
    robot decided is not helped by a list of times a model timed out, and a reader debugging an
    outage is not helped by scrolling past a hundred sound decisions to find the failures.
    """

    @classmethod
    def decision_group(cls, status: str) -> str:
        """Which of the three kinds a ledger row is: still running, broken, or concluded.

        Defined once, next to the code that writes these statuses, so the page and anything else
        reading the ledger cannot drift into disagreeing about what a row means.
        """
        value = str(status or "").upper()
        if value == cls.IN_PROGRESS:
            return "running"
        if value in cls.FAILED_STATUSES:
            return "failed"
        return "concluded"

    def _cancel_key(self, decision_id: int) -> tuple[str, int]:
        # Ids restart at one in every database, so an id alone names a different decision in each
        # of them. Keyed by the file as well, a cancellation can only ever stop the row it was for.
        return (str(Path(self.path).resolve()), int(decision_id))

    def cancel_decision(self, decision_id: int) -> None:
        with _CANCELLED_LOCK:
            _CANCELLED.add(self._cancel_key(decision_id))

    def is_cancelled(self, decision_id: int | None) -> bool:
        if decision_id is None:
            return False
        with _CANCELLED_LOCK:
            return self._cancel_key(decision_id) in _CANCELLED

    IN_PROGRESS = "STARTED"

    SCREENED_OUT = "SCREENED_OUT"
    """A round that was not run, because the cheap screener said nothing had changed since the last.

    Deliberately not a HOLD: nothing was analysed, and a ledger that records an unexamined market
    as a judgement is a ledger that lies about what was done. The row carries the previous verdict
    it is standing on and what the screener compared, so the saving is auditable rather than silent.
    """

    VENUE_ACTION_SQL = (
        "NOT (COALESCE(json_extract(result_json, '$.status'), '') IN ('NO_ACTION', 'NO_POSITION')"
        " OR action = 'RISK_REJECTED' OR action LIKE '%\\_FAILED' ESCAPE '\\')"
    )
    """Whether an execution row records something a venue actually did.

    A decision that held, found nothing to sell, was refused by a rule, or failed before the order
    was accepted changed nothing anywhere - deleting its record leaves no effect behind and no
    balance to disagree with. A fill, a cancellation or a redemption did, and is what this protects.
    """
    """The status a decision carries while the code that opened it is still running.

    Something is holding that id and will come back to complete it. Removing the row makes that
    completion an update of nothing - silently, because an UPDATE that matches no row is not an
    error - so the reasoning and the outcome are simply lost and the operator is left believing
    they tidied up a stuck entry.
    """

    def readable(self, decision_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT readable_json FROM decision_ledger WHERE id = ?", (int(decision_id),)
        ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def save_readable(self, decision_id: int, value: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE decision_ledger SET readable_json = ? WHERE id = ?",
            (json.dumps(value, ensure_ascii=False), int(decision_id)),
        )
        self.connection.commit()

    def reclaim_log(self) -> dict[str, Any]:
        """Drain the write-ahead log and hand the space back, after something large was deleted.

        SQLite checkpoints on its own, but only when a writer happens to cross a page threshold and
        no reader is holding an older view of the database. A console that deletes a whole ledger
        and then sits idle satisfies neither, so the log stays at whatever size that delete needed -
        here, 290 MB - until something asks. This asks.
        """
        try:
            busy, written, moved = self.connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        except sqlite3.OperationalError as error:
            return {"reclaimed": False, "why": str(error)}
        return {"reclaimed": not busy, "pages_written": written, "pages_moved": moved}

    def forget_decisions(
        self,
        *,
        decision_ids: list[int] | None = None,
        status: str = "",
        platform: str = "",
    ) -> dict[str, Any]:
        """Remove decisions and the reasoning behind them, once an operator says they are junk.

        This is not housekeeping. Everything here is read back to decide what happens next -
        calibration against settled outcomes, the ranking that picks which provider to ask first,
        and the history handed to the model - so a run that failed for a reason since fixed does
        not merely look untidy, it goes on shaping behaviour with evidence that was never about
        the thing it is now counted against.

        A decision still in progress is removed too, and the analysis behind it is told to stop.

        What actually happened at a venue is not removed with it. The account's cash and positions
        are kept as their own state rather than derived from these rows, so deleting an executed
        action would take away the record while leaving its effect - a ledger that disagrees with
        the balance is worse than an untidy one. Those rows are reported and left alone.

        Every decision writes an execution row, including the ones that decided to do nothing, so
        "has an execution row" protected all of them: every hold in the ledger refused to be
        deleted, which is not what the protection is for.
        """
        selected = self._decisions_to_forget(decision_ids, status, platform)
        if not selected:
            return {
                "decisions": 0, "provider_turns": 0, "agent_steps": 0,
                "kept_executed": 0, "cancelled_in_progress": 0,
            }
        marks = ",".join("?" for _ in selected)
        running = {
            int(row[0])
            for row in self.connection.execute(
                f"SELECT id FROM decision_ledger WHERE id IN ({marks}) AND status = ?",
                [*selected, self.IN_PROGRESS],
            )
        }
        # A row still marked as running is deleted like any other, and the analysis behind it is
        # told to stop. Refusing it instead left rows stuck forever whenever the process that held
        # them was gone - a restart mid-analysis leaves a STARTED row nothing will ever complete -
        # and letting the analysis carry on after the row was removed would be spending model calls
        # on a conclusion with nowhere to go.
        for decision_id in running:
            self.cancel_decision(decision_id)
        executed = self.connection.execute(
            f"SELECT COUNT(*) FROM execution_actions "
            f"WHERE decision_id IN ({marks}) AND {self.VENUE_ACTION_SQL}",
            selected,
        ).fetchone()[0]
        keep = [
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT decision_id FROM execution_actions "
                f"WHERE decision_id IN ({marks}) AND {self.VENUE_ACTION_SQL}",
                selected,
            )
        ]
        removable = [item for item in selected if item not in set(keep)]
        if not removable:
            return {
                "decisions": 0, "provider_turns": 0, "agent_steps": 0,
                "kept_executed": int(executed), "cancelled_in_progress": len(running),
                "kept_ids": [int(item) for item in keep],
            }
        marks = ",".join("?" for _ in removable)
        turns = self.connection.execute(
            f"DELETE FROM provider_turns WHERE decision_id IN ({marks})", removable
        ).rowcount
        steps = self.connection.execute(
            f"DELETE FROM agent_steps WHERE decision_id IN ({marks})", removable
        ).rowcount
        decisions = self.connection.execute(
            f"DELETE FROM decision_ledger WHERE id IN ({marks})", removable
        ).rowcount
        self.connection.commit()
        return {
            "decisions": int(decisions),
            "provider_turns": int(turns),
            "agent_steps": int(steps),
            "kept_executed": int(executed),
            "cancelled_in_progress": len(running),
            "kept_ids": [int(item) for item in keep],
        }

    def _decisions_to_forget(
        self, decision_ids: list[int] | None, status: str, platform: str
    ) -> list[int]:
        """Resolve what was asked for into ids, refusing a request that names nothing.

        An empty filter would match the whole ledger. Deleting everything is a thing someone may
        genuinely want, but it is not a thing they should get by leaving a box blank.
        """
        if decision_ids:
            return [int(item) for item in decision_ids]
        clauses, values = [], []
        if status:
            clauses.append("status = ?")
            values.append(str(status))
        if platform:
            clauses.append("platform = ?")
            values.append(str(platform))
        if not clauses:
            raise ValueError(
                "Name which decisions to forget: ids, a status, or a platform. "
                "An unfiltered request would delete the entire ledger."
            )
        rows = self.connection.execute(
            f"SELECT id FROM decision_ledger WHERE {' AND '.join(clauses)}", values
        ).fetchall()
        return [int(row[0]) for row in rows]

    def settled_decisions(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Decisions whose outcome token was later redeemed, so the truth is known.

        Only platforms that report settlement produce these rows; where a plugin declares
        settlement_status false there is nothing to calibrate against and the list stays empty.
        """
        rows = self.connection.execute(
            """
            SELECT ledger.created_at, ledger.platform, ledger.token_id,
                   ledger.final_decision_json, ledger.proposed_decision_json,
                   ledger.status, redeem.request_json, ledger.provider
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
        for (
            created_at,
            platform,
            token_id,
            final_json,
            proposed_json,
            status,
            request_json,
            provider,
        ) in rows:
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
                    "provider": str(provider or ""),
                    "action": str(decision.get("action", "")).upper(),
                    "estimated_probability": float(decision["estimated_probability"]),
                    "confidence": float(decision.get("confidence") or 0.0),
                    "priors": [str(item) for item in decision.get("priors") or []],
                    "won": bool(request["winning"]),
                }
            )
        return results

    def provider_delivery(self, *, limit: int = 5000) -> dict[str, dict[str, int]]:
        """How reliably each provider returned a usable answer at all."""
        rows = self.connection.execute(
            """
            SELECT provider, status, COUNT(*)
            FROM (
                SELECT provider, status FROM provider_turns
                ORDER BY created_at DESC LIMIT ?
            )
            GROUP BY provider, status
            """,
            (int(limit),),
        ).fetchall()
        totals: dict[str, dict[str, int]] = {}
        for provider, status, count in rows:
            entry = totals.setdefault(str(provider), {"turns": 0, "ok": 0, "errors": 0})
            entry["turns"] += int(count)
            if str(status).upper() == "OK":
                entry["ok"] += int(count)
            else:
                entry["errors"] += int(count)
        return totals

    def provider_tool_effort(self, *, limit: int = 5000) -> dict[str, dict[str, int]]:
        """Tool steps each provider spent, as a proxy for how efficiently it reaches an answer."""
        rows = self.connection.execute(
            """
            SELECT provider, COUNT(*), COUNT(DISTINCT decision_id)
            FROM (
                SELECT provider, decision_id FROM agent_steps
                ORDER BY created_at DESC LIMIT ?
            )
            GROUP BY provider
            """,
            (int(limit),),
        ).fetchall()
        return {
            str(provider): {"steps": int(steps), "decisions": int(decisions or 0)}
            for provider, steps, decisions in rows
        }

    def recent_decisions_for_review(
        self, *, limit: int = 20, exclude_reviewed: bool = True
    ) -> list[dict[str, Any]]:
        """Completed decisions a second provider can grade."""
        query = """
            SELECT ledger.id, ledger.provider, ledger.platform, ledger.token_id,
                   ledger.final_decision_json, ledger.proposed_decision_json, ledger.context_json
            FROM decision_ledger AS ledger
            WHERE ledger.provider != '' AND ledger.status = 'OK'
        """
        if exclude_reviewed:
            query += """
              AND NOT EXISTS (
                SELECT 1 FROM provider_reviews AS seen WHERE seen.decision_id = ledger.id
              )
            """
        query += " ORDER BY ledger.created_at DESC LIMIT ?"
        rows = self.connection.execute(query, (int(limit),)).fetchall()
        results = []
        for row in rows:
            try:
                decision = json.loads(row[4] or row[5] or "{}")
                context = json.loads(row[6] or "{}")
            except json.JSONDecodeError:
                continue
            results.append(
                {
                    "decision_id": int(row[0]),
                    "provider": str(row[1]),
                    "platform": str(row[2]),
                    "token_id": str(row[3]),
                    "decision": decision,
                    "context": context,
                }
            )
        return results

    def record_provider_review(
        self,
        *,
        decision_id: int,
        subject_provider: str,
        reviewer_provider: str,
        scores: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO provider_reviews(
                created_at, decision_id, subject_provider, reviewer_provider, scores_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                int(decision_id),
                subject_provider,
                reviewer_provider,
                self._json(scores),
            ),
        )
        self.connection.commit()

    def provider_review_scores(self, *, limit: int = 2000) -> dict[str, dict[str, float]]:
        """Averaged peer scores per provider, excluding anything a provider graded itself."""
        rows = self.connection.execute(
            """
            SELECT subject_provider, scores_json FROM provider_reviews
            WHERE subject_provider != reviewer_provider
            ORDER BY created_at DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        totals: dict[str, dict[str, float]] = {}
        for provider, scores_json in rows:
            try:
                scores = json.loads(scores_json or "{}")
            except json.JSONDecodeError:
                continue
            overall = scores.get("overall")
            if overall is None:
                continue
            entry = totals.setdefault(str(provider), {"reviews": 0.0, "total": 0.0})
            entry["reviews"] += 1
            entry["total"] += float(overall)
        return {
            provider: {
                "reviews": int(entry["reviews"]),
                "average": round(entry["total"] / entry["reviews"], 4),
            }
            for provider, entry in totals.items()
            if entry["reviews"]
        }

    def merge_decision_context(self, decision_id: int, extra: dict[str, Any]) -> None:
        """Fold what a round learned back into the context it was working from.

        A round is recorded before it runs, because a round that dies has to leave a trace. What it
        then reads - a deadline, a spread, a resolution it went and checked - is part of the same
        picture, and a record that shows only the unpriced list it started with tells the operator
        the round was flying blind when it was not.
        """
        row = self.connection.execute(
            "SELECT context_json FROM decision_ledger WHERE id = ?", (int(decision_id),)
        ).fetchone()
        if row is None:
            return
        try:
            context = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            context = {}
        if not isinstance(context, dict):
            context = {}
        context.update(extra)
        self.connection.execute(
            "UPDATE decision_ledger SET context_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(context, ensure_ascii=False), int(time.time() * 1000), int(decision_id)),
        )
        self.connection.commit()

    INSTRUCTION_KINDS = ("fund_condition", "strategy_note", "reminder", "remark")
    """What somebody paying can attach to the money, and the one thing that binds nothing.

    A condition on the money ("spend it within three days", "only on sports"), a change to how the
    robot should trade, or something to keep in mind - those three reach the rounds. A remark - a
    thank-you, a greeting, a sentence about nothing in particular - is written down and never handed
    to one: pasting a note the robot cannot act on into every prompt is how prompts turn to noise.
    It is kept because the operator wrote it and will look for it, not because anything obeys it.
    """

    def record_instruction(
        self,
        *,
        platform: str,
        source: str,
        raw_text: str,
        kind: str,
        headline: str,
        instruction: str,
        conditions: dict[str, Any] | None = None,
        status: str = "active",
    ) -> int:
        """Keep something the operator attached to their money, in the terms it will be applied in.

        The words they wrote are kept as well as what was made of them: when the robot later says a
        condition was met, the operator has to be able to check that against what they actually
        said, not against a paraphrase nobody can audit.
        """
        now = int(time.time() * 1000)
        cursor = self.connection.execute(
            """
            INSERT INTO operator_instructions(
                created_at, updated_at, platform, source, raw_text, kind, headline,
                instruction, conditions_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, now, str(platform), str(source), str(raw_text), str(kind), str(headline),
             str(instruction), json.dumps(conditions or {}, ensure_ascii=False), str(status)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def active_instructions(self, platform: str = "") -> list[dict[str, Any]]:
        """What the operator is still owed, which is what every round has to work within."""
        clauses, values = ["status = 'active'"], []
        if platform:
            clauses.append("(platform = ? OR platform = '')")
            values.append(str(platform))
        rows = self.connection.execute(
            f"SELECT id, created_at, platform, source, raw_text, kind, headline, instruction,"
            f" conditions_json, progress FROM operator_instructions WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at",
            values,
        ).fetchall()
        return [
            {
                "id": int(row[0]), "created_at": int(row[1]), "platform": row[2], "source": row[3],
                "raw_text": row[4], "kind": row[5], "headline": row[6], "instruction": row[7],
                "conditions": _instruction_conditions(row[8]), "progress": row[9],
            }
            for row in rows
        ]

    def resolve_instruction(self, instruction_id: int, *, status: str, resolution: str) -> None:
        """Mark one as done, expired or dropped, with the reason it stopped applying."""
        now = int(time.time() * 1000)
        self.connection.execute(
            "UPDATE operator_instructions SET status = ?, resolution = ?, updated_at = ?,"
            " checked_at = ? WHERE id = ?",
            (str(status), str(resolution)[:600], now, now, int(instruction_id)),
        )
        self.connection.commit()

    def note_instruction_check(self, instruction_ids: list[int]) -> None:
        """Remember that these were looked at, so a quiet round is distinguishable from no round."""
        if not instruction_ids:
            return
        marks = ",".join("?" for _ in instruction_ids)
        self.connection.execute(
            f"UPDATE operator_instructions SET checked_at = ? WHERE id IN ({marks})",
            [int(time.time() * 1000), *[int(item) for item in instruction_ids]],
        )
        self.connection.commit()

    def note_instruction_progress(self, instruction_id: int, progress: str) -> None:
        """Keep how far along one is, because "still open" is not an answer to "how is it going"."""
        self.connection.execute(
            "UPDATE operator_instructions SET progress = ?, updated_at = ?, checked_at = ?"
            " WHERE id = ?",
            (str(progress)[:400], int(time.time() * 1000), int(time.time() * 1000),
             int(instruction_id)),
        )
        self.connection.commit()

    def forget_instruction(self, instruction_id: int) -> bool:
        """Drop one entirely, so that nothing downstream is working to it any more.

        Deleted rather than closed: a closed instruction is one the robot says it carried out, and
        the operator taking a note back is not that. What they asked for is gone from the record and
        from every prompt built after this, which is the only reading of "delete" that would not
        leave the robot quietly still obeying it.
        """
        cursor = self.connection.execute(
            "DELETE FROM operator_instructions WHERE id = ?", (int(instruction_id),)
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def instructions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Everything the operator has attached, open and closed, newest first."""
        rows = self.connection.execute(
            "SELECT id, created_at, updated_at, platform, source, raw_text, kind, headline,"
            " instruction, conditions_json, status, resolution, progress, checked_at"
            " FROM operator_instructions ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        columns = ("id", "created_at", "updated_at", "platform", "source", "raw_text", "kind",
                   "headline", "instruction", "conditions", "status", "resolution", "progress",
                   "checked_at")
        items = []
        for row in rows:
            item = dict(zip(columns, row))
            item["conditions"] = _instruction_conditions(item["conditions"])
            items.append(item)
        return items

    def decisions_since_instruction(self, platform: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """What this platform decided lately, in the terms a promise is judged against.

        Whether "spend it within three days" happened is a question about trades, not about
        reasoning, so this carries the action, the size and what the venue did with it - and
        nothing else, because the review that reads it costs a model call.
        """
        rows = self.connection.execute(
            """
            SELECT created_at, context_json, final_decision_json, execution_json, status
            FROM decision_ledger WHERE platform = ? AND strategy_name NOT LIKE '%discovery'
            ORDER BY created_at DESC LIMIT ?
            """,
            (str(platform), max(1, int(limit))),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for created_at, context_json, final_json, execution_json, status in rows:
            try:
                context = json.loads(context_json or "{}")
                final = json.loads(final_json or "{}")
                execution = json.loads(execution_json or "{}")
            except json.JSONDecodeError:
                continue
            items.append({
                "at_ms": int(created_at),
                "market": (context.get("market") or {}).get("title", "") if isinstance(context, dict) else "",
                "action": (final or {}).get("action", ""),
                "notional_usdt": (final or {}).get("notional_usdt"),
                "execution": (execution or {}).get("status", ""),
                "status": status,
            })
        return items

    def queue_market_screening(
        self, *, platform: str, candidates: list[dict[str, Any]],
        topic_ids: list[str] | None = None, observed_at: int | None = None
    ) -> int:
        """Retain every listed contract until it receives a typed screening answer.

        A material price/status change wakes an explicitly skipped contract. Unchanged contracts
        are revisited on their own due time; listing order cannot repeatedly starve the tail.
        """
        now = int(observed_at if observed_at is not None else time.time() * 1000)
        queued = 0
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "")
            topic_id = str(candidate.get("topic_id") or "")
            if not candidate_id or not topic_id:
                continue
            key = str(candidate.get("material_key") or "")
            self.connection.execute(
                """
                INSERT INTO market_screening_queue (
                    platform, candidate_id, topic_id, market_id, candidate_json, material_key,
                    first_seen_at, last_seen_at, next_due_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, candidate_id) DO UPDATE SET
                    topic_id = excluded.topic_id,
                    market_id = excluded.market_id,
                    active = 1,
                    candidate_json = excluded.candidate_json,
                    material_key = excluded.material_key,
                    last_seen_at = excluded.last_seen_at,
                    next_due_at = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN excluded.next_due_at
                        ELSE market_screening_queue.next_due_at END,
                    assessment_json = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN '{}' ELSE market_screening_queue.assessment_json END,
                    last_screened_at = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN 0 ELSE market_screening_queue.last_screened_at END,
                    failure_count = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN 0 ELSE market_screening_queue.failure_count END,
                    last_error = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN '' ELSE market_screening_queue.last_error END,
                    skip_until = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN 0 ELSE market_screening_queue.skip_until END,
                    skip_reason = CASE
                        WHEN market_screening_queue.material_key != excluded.material_key
                        THEN '' ELSE market_screening_queue.skip_reason END
                """,
                (
                    str(platform), candidate_id, topic_id,
                    str(candidate.get("market_id") or ""), self._json(candidate), key,
                    now, now, now,
                ),
            )
            queued += 1
        by_topic: dict[str, list[str]] = {}
        for candidate in candidates:
            topic_id = str(candidate.get("topic_id") or "")
            candidate_id = str(candidate.get("candidate_id") or "")
            if topic_id and candidate_id:
                by_topic.setdefault(topic_id, []).append(candidate_id)
        for topic_id in topic_ids or []:
            keep = by_topic.get(str(topic_id), [])
            if keep:
                placeholders = ",".join("?" for _ in keep)
                self.connection.execute(
                    f"""UPDATE market_screening_queue SET active = 0
                        WHERE platform = ? AND topic_id = ? AND candidate_id NOT IN ({placeholders})""",
                    (str(platform), str(topic_id), *keep),
                )
            else:
                self.connection.execute(
                    "UPDATE market_screening_queue SET active = 0 WHERE platform = ? AND topic_id = ?",
                    (str(platform), str(topic_id)),
                )
        self.connection.commit()
        return queued

    def due_market_screening(
        self, *, platform: str, limit: int, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        rows = self.connection.execute(
            """
            SELECT candidate_json, assessment_json, last_screened_at FROM market_screening_queue
            WHERE platform = ? AND active = 1 AND next_due_at <= ? AND skip_until <= ?
            ORDER BY next_due_at, first_seen_at, rowid LIMIT ?
            """,
            (str(platform), now, now, max(1, int(limit))),
        ).fetchall()
        candidates = []
        for raw, assessment_raw, screened_at in rows:
            candidate = json.loads(raw)
            if screened_at:
                assessment = json.loads(assessment_raw or "{}")
                if isinstance(assessment, dict) and assessment.get("action"):
                    candidate["prior_screen"] = {
                        "action": assessment["action"],
                        "confidence": assessment.get("confidence"),
                    }
            candidates.append(candidate)
        return candidates

    def recent_market_decisions(
        self, *, platform: str, market_ids: list[str], limit_per_market: int = 2
    ) -> dict[str, list[dict[str, Any]]]:
        """Only decisions for the exact contract, never another contract in its event."""
        ids = list(dict.fromkeys(str(item) for item in market_ids if str(item)))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""SELECT market_id, final_decision_json FROM (
                  SELECT market_id, final_decision_json,
                         ROW_NUMBER() OVER (
                             PARTITION BY market_id ORDER BY created_at DESC, id DESC
                         ) AS rank_in_market
                  FROM decision_ledger
                  WHERE platform = ? AND market_id IN ({marks})
                    AND final_decision_json IS NOT NULL
                ) WHERE rank_in_market <= ?""",
            (str(platform), *ids, max(1, int(limit_per_market))),
        ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for market_id, raw in rows:
            items = result.setdefault(str(market_id), [])
            if len(items) >= max(1, int(limit_per_market)):
                continue
            try:
                decision = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(decision, dict) and decision.get("action"):
                items.append({
                    "action": str(decision["action"]),
                    "revisit_when": str(decision.get("revisit_when") or "")[:100],
                })
        return result

    def next_market_screening_at(self, *, platform: str) -> int | None:
        row = self.connection.execute(
            """SELECT MIN(MAX(next_due_at, skip_until)) FROM market_screening_queue
               WHERE platform = ? AND active = 1""", (str(platform),)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def complete_market_screening(
        self, *, platform: str, candidate_id: str, assessment: dict[str, Any] | None,
        error: str = "", now_ms: int | None = None,
    ) -> None:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if assessment is None:
            self.connection.execute(
                """
                UPDATE market_screening_queue SET
                    failure_count = failure_count + 1,
                    last_error = ?,
                    next_due_at = ? + MIN(300000, 5000 * (1 << MIN(failure_count, 6)))
                WHERE platform = ? AND candidate_id = ?
                """,
                (str(error)[:300], now, str(platform), str(candidate_id)),
            )
        else:
            self.connection.execute(
                """
                UPDATE market_screening_queue SET
                    assessment_json = ?, last_screened_at = ?, next_due_at = ?,
                    failure_count = 0, last_error = ''
                WHERE platform = ? AND candidate_id = ?
                """,
                (self._json(assessment), now, now + 15 * 60 * 1000,
                 str(platform), str(candidate_id)),
            )
        self.connection.commit()

    def market_screening_for_topics(
        self, *, platform: str, topic_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not topic_ids:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(topic_ids), 500):
            chunk = topic_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""SELECT topic_id, candidate_id, candidate_json, assessment_json,
                           last_screened_at, last_error, next_due_at, skip_until
                    FROM market_screening_queue
                    WHERE platform = ? AND active = 1 AND topic_id IN ({placeholders})""",
                (str(platform), *chunk),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row[0]), []).append({
                    "candidate_id": str(row[1]),
                    "candidate": json.loads(row[2]),
                    "assessment": json.loads(row[3] or "{}"),
                    "last_screened_at": int(row[4]),
                    "last_error": str(row[5]),
                    "next_due_at": int(row[6]),
                    "skip_until": int(row[7]),
                })
        return result

    def market_screening_counts(self, *, platform: str) -> dict[str, int]:
        now = int(time.time() * 1000)
        row = self.connection.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN last_screened_at > 0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN next_due_at <= ? AND skip_until <= ? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN last_error != '' THEN 1 ELSE 0 END)
               FROM market_screening_queue WHERE platform = ? AND active = 1""",
            (now, now, str(platform)),
        ).fetchone()
        return {
            "known": int(row[0] or 0), "screened": int(row[1] or 0),
            "due": int(row[2] or 0), "failed": int(row[3] or 0),
        }

    def skip_market_screening(
        self, *, platform: str, candidate_id: str, for_seconds: int, reason: str
    ) -> bool:
        """Only a named model instruction can defer a known contract's coarse screening."""
        seconds = int(for_seconds)
        if not 60 <= seconds <= 2592000 or not str(reason).strip():
            return False
        until = int(time.time() * 1000) + seconds * 1000
        cursor = self.connection.execute(
            """UPDATE market_screening_queue SET
                   skip_until = ?, skip_material_key = material_key, skip_reason = ?,
                   next_due_at = MIN(next_due_at, ?)
               WHERE platform = ? AND candidate_id = ? AND active = 1""",
            (until, str(reason)[:300], until, str(platform), str(candidate_id)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def schedule_market_review(
        self, *, platform: str, topic_id: str, market_id: str,
        token_id: str, after_seconds: int, reason: str,
        source_decision_id: int | None = None,
    ) -> int:
        if not topic_id or not market_id or not token_id or not reason.strip():
            raise ValueError("a scheduled review needs exact topic, market and token IDs and a reason")
        seconds = int(after_seconds)
        if not 1 <= seconds <= 2592000:
            raise ValueError("scheduled review delay must be within 30 days")
        now = int(time.time() * 1000)
        due = now + seconds * 1000
        existing = self.connection.execute(
            """SELECT id FROM scheduled_market_reviews
               WHERE platform = ? AND topic_id = ? AND market_id = ? AND token_id = ?
                 AND completed_at = 0 AND due_at <= ?
               ORDER BY due_at LIMIT 1""",
            (platform, topic_id, market_id, token_id, due),
        ).fetchone()
        if existing:
            return int(existing[0])
        cursor = self.connection.execute(
            """INSERT INTO scheduled_market_reviews (
                   platform, topic_id, market_id, token_id, due_at, reason,
                   source_decision_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (platform, topic_id, market_id, token_id, due,
             reason[:300], source_decision_id, now),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def next_market_review_at(self, *, platform: str) -> int | None:
        row = self.connection.execute(
            """SELECT MIN(due_at) FROM scheduled_market_reviews
               WHERE platform = ? AND completed_at = 0""", (str(platform),)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def due_market_reviews(
        self, *, platform: str, limit: int = 1, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        rows = self.connection.execute(
            """SELECT id, topic_id, market_id, token_id, due_at, reason
               FROM scheduled_market_reviews
               WHERE platform = ? AND completed_at = 0 AND due_at <= ?
               ORDER BY due_at, id LIMIT ?""",
            (str(platform), now, max(1, int(limit))),
        ).fetchall()
        return [
            {"id": int(row[0]), "topic_id": str(row[1]), "market_id": str(row[2]),
             "token_id": str(row[3]), "due_at": int(row[4]), "reason": str(row[5])}
            for row in rows
        ]

    def finish_market_review(self, review_id: int, *, error: str = "") -> None:
        now = int(time.time() * 1000)
        if error:
            # An outage is not a completed appointment. Retry independently of the broad scan.
            self.connection.execute(
                """UPDATE scheduled_market_reviews SET due_at = ?, last_error = ?
                   WHERE id = ? AND completed_at = 0""",
                (now + 60_000, str(error)[:300], int(review_id)),
            )
        else:
            self.connection.execute(
                """UPDATE scheduled_market_reviews SET completed_at = ?, last_error = ''
                   WHERE id = ? AND completed_at = 0""",
                (now, int(review_id)),
            )
        self.connection.commit()

    def save_survey_plan(
        self, *, platform: str, queries: list[str], next_scan_seconds: int, reason: str
    ) -> None:
        """Keep what the agent asked for next, so the plan outlives the round that made it.

        Held in the database rather than in the process because a plan that vanishes on restart
        reverts the robot to the fixed cadence and the fixed listing without saying so - and the
        symptom of that, a survey quietly back to its defaults, is indistinguishable from the agent
        having asked for the defaults.
        """
        self.connection.execute(
            """
            INSERT INTO survey_plans(platform, queries_json, next_scan_seconds, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                queries_json = excluded.queries_json,
                next_scan_seconds = excluded.next_scan_seconds,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                str(platform),
                json.dumps([str(item) for item in queries][:8], ensure_ascii=False),
                max(0, int(next_scan_seconds)),
                str(reason)[:400],
                int(time.time() * 1000),
            ),
        )
        self.connection.commit()

    def survey_plan(self, platform: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT queries_json, next_scan_seconds, reason, updated_at, resume_json"
            " FROM survey_plans WHERE platform = ?",
            (str(platform),),
        ).fetchone()
        if row is None:
            return {
                "queries": [], "next_scan_seconds": 0, "reason": "", "updated_at": 0,
                "resume": {},
            }
        try:
            queries = json.loads(row[0])
        except json.JSONDecodeError:
            queries = []
        try:
            resume = json.loads(row[4] or "{}")
        except json.JSONDecodeError:
            resume = {}
        return {
            "queries": [str(item) for item in queries] if isinstance(queries, list) else [],
            "next_scan_seconds": int(row[1]),
            "reason": str(row[2]),
            "updated_at": int(row[3]),
            "resume": resume if isinstance(resume, dict) else {},
        }

    def save_survey_resume(self, platform: str, resume: dict[str, Any]) -> None:
        """Persist only the bounded scan cursor; pacing and query plans remain independent."""
        now = int(time.time() * 1000)
        self.connection.execute(
            """
            INSERT INTO survey_plans(
                platform, queries_json, next_scan_seconds, reason, resume_json, updated_at
            ) VALUES (?, '[]', 0, '', ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                resume_json = excluded.resume_json,
                updated_at = excluded.updated_at
            """,
            (str(platform), self._json(resume), now),
        )
        self.connection.commit()

    def clear_survey_resume(self, platform: str) -> None:
        self.connection.execute(
            "UPDATE survey_plans SET resume_json = '{}' WHERE platform = ?",
            (str(platform),),
        )
        self.connection.commit()

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
