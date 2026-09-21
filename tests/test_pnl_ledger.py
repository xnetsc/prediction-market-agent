from ._support import *

import json

from prediction_market_agent.core.domain import Position
from prediction_market_agent.runtime.pnl import pnl_events, pnl_positions, pnl_summary


class LedgerTransport:
    def __init__(self) -> None:
        self.orders = 0

    def get_quote(self, **values):
        return {
            "quoteId": f"quote-{self.orders + 1}",
            "averagePrice": values["reference_price"],
            "expireAt": 4_102_444_800_000,
        }

    def place_order(self, **values):
        self.orders += 1
        return {"orderId": f"order-{self.orders}", "status": "FILLED"}

    def cancel_orders(self, order_ids):
        return {"canceled": order_ids}

    def redeem(self, outcome_ids):
        return {"status": "COMPLETED", "outcomeIds": outcome_ids}

    def transfer(self, direction, amount):
        return {"status": "COMPLETED", "direction": direction, "amount": amount}


class ProfitAndLossLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "sessions.sqlite3"
        self.state_path = self.root / "state.json"
        self.memory = SessionMemory(self.db)
        self.state = AccountState(starting_capital=100.0, cash=100.0)
        self.gateway = ExecutionGateway(
            self.state,
            platform="venue",
            write_transport=LedgerTransport(),
            account_mode="live",
            currency="USDC",
            pnl_event_sink=self.memory.record_pnl_event,
        )

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def quote(self, side="BUY", price=0.4, quantity=10):
        return self.gateway.get_quote(
            token_id="yes",
            side=side,
            price=price,
            quantity=quantity,
            fee_bps=50,
            market_topic_id="topic",
            market_id="market",
            symbol="EVENT/USDC",
            direction="YES",
        )

    def test_transfers_reconcile_cash_without_becoming_profit(self) -> None:
        self.gateway.transfer("INBOUND", 20.0)
        self.gateway.place_order(self.quote(), decision_id=7)
        self.gateway.mark("yes", 0.6)
        StateStore(self.state_path, None).save(self.state)

        answer = pnl_summary(
            self.db, {"venue": self.state_path}, current_mode="live"
        )
        account = answer["accounts"][0]
        self.assertEqual(account["net_external_flow"], 20.0)
        self.assertAlmostEqual(account["realized_pnl"], -0.02)
        self.assertAlmostEqual(account["unrealized_pnl"], 2.0)
        self.assertAlmostEqual(account["total_pnl"], 1.98)
        self.assertNotAlmostEqual(account["total_pnl"], 21.98)
        self.assertEqual(answer["totals"][0]["currency"], "USDC")

        rows = pnl_events(self.db, limit=20, offset=0)["items"]
        inbound = next(row for row in rows if row["event_type"] == "TRANSFER_IN")
        fill = next(row for row in rows if row["event_type"] == "BUY_FILL")
        self.assertEqual(inbound["realized_pnl_delta"], 0.0)
        self.assertEqual(inbound["external_flow_delta"], 20.0)
        self.assertEqual(fill["decision_id"], 7)
        self.assertAlmostEqual(fill["realized_pnl_delta"], -0.02)

    def test_unknown_cost_basis_is_null_not_zero_profit(self) -> None:
        self.gateway.place_order(self.quote(side="SELL", price=0.7, quantity=2))
        row = pnl_events(self.db, limit=1, offset=0)["items"][0]
        self.assertEqual(row["event_type"], "SELL_FILL")
        self.assertIsNone(row["cost_basis_delta"])
        self.assertIsNone(row["realized_pnl_delta"])
        self.assertEqual(row["evidence_status"], "partial")
        self.assertEqual(self.state.realized_pnl, 0.0)
        StateStore(self.state_path, None).save(self.state)
        account = pnl_summary(
            self.db, {"venue": self.state_path}, current_mode="live"
        )["accounts"][0]
        self.assertIsNone(account["realized_pnl"])
        self.assertIsNone(account["total_pnl"])
        self.assertEqual(account["known_realized_pnl"], 0.0)
        self.assertEqual(account["uncertain_pnl_events"], 1)

    def test_sell_beyond_recorded_position_does_not_invent_profit(self) -> None:
        self.gateway.place_order(self.quote(quantity=1))
        self.gateway.place_order(self.quote(side="SELL", price=0.7, quantity=2))
        row = pnl_events(self.db, limit=1, offset=0)["items"][0]
        self.assertIsNone(row["cost_basis_delta"])
        self.assertIsNone(row["realized_pnl_delta"])
        self.assertEqual(row["evidence_status"], "partial")
        self.assertIn("exceeds", row["metadata"]["unknown"])
        self.assertAlmostEqual(self.state.realized_pnl, -0.002)
        self.assertNotIn("yes", self.state.positions)

    def test_old_position_without_mark_time_keeps_floating_pnl_unknown(self) -> None:
        self.state.positions["old"] = Position(
            token_id="old",
            market_topic_id="topic",
            market_id="market",
            symbol="OLD/USDC",
            direction="YES",
            quantity=4,
            average_price=0.2,
            mark_price=0.8,
            opened_at=1,
        )
        StateStore(self.state_path, None).save(self.state)
        summary = pnl_summary(
            self.db, {"venue": self.state_path}, current_mode="live"
        )
        self.assertIsNone(summary["accounts"][0]["unrealized_pnl"])
        self.assertIsNone(summary["accounts"][0]["total_pnl"])
        self.assertFalse(summary["accounts"][0]["historical_breakdown_complete"])
        self.assertFalse(summary["totals"][0]["complete"])
        position = pnl_positions(
            {"venue": self.state_path}, current_mode="live"
        )["items"][0]
        self.assertIsNone(position["mark_price"])
        self.assertIsNone(position["unrealized_pnl"])

    def test_event_order_key_is_idempotent(self) -> None:
        event = {
            "platform": "venue",
            "account_mode": "paper",
            "currency": "USDC",
            "event_type": "BUY_FILL",
            "order_id": "same-order",
        }
        first = self.memory.record_pnl_event(event)
        second = self.memory.record_pnl_event(event)
        self.assertEqual(first, second)
        self.assertEqual(pnl_events(self.db, limit=20, offset=0)["count"], 1)

    def test_runtime_incidents_are_append_only_evidence(self) -> None:
        incident = self.memory.record_runtime_incident(
            platform="venue",
            stage="market_selection",
            severity="error",
            message="schema violation",
            fallback="prescore",
            details={"provider": "openrouter"},
        )
        row = self.memory.connection.execute(
            "SELECT stage, message, fallback, details_json FROM runtime_incidents WHERE id = ?",
            (incident,),
        ).fetchone()
        self.assertEqual(row[:3], ("market_selection", "schema violation", "prescore"))
        self.assertEqual(json.loads(row[3]), {"provider": "openrouter"})
