"""What somebody attached to their money, from the words they typed to the round that obeys them.

The chain has four links and each one can break quietly. A note can be read and then not kept; a
kept instruction can fail to reach the prompt; one that reached it can stay binding after it was
carried out; and a deleted one can go on deciding things. None of those show up as an error - they
show up as a robot that ignored what somebody asked for while reporting that everything is fine.
"""
from ._support import *
import json

from prediction_market_agent.core.config import Config
from prediction_market_agent.runtime.memory import SessionMemory
from prediction_market_agent.runtime.market_tools import MarketToolset
from prediction_market_agent.runtime.operator_instructions import (
    OperatorInstructions, READ_SCHEMA, REVIEW_SCHEMA, by_urgency, instruction_detail,
)


class Model:
    """Answers whatever the test put in the queue, and records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def run(self, payload, **options):
        self.asked.append({"payload": payload, **options})
        if not self.answers:
            raise AssertionError("the model was asked more times than the test expected")
        return SimpleNamespace(value=self.answers.pop(0))


class Plugin:
    name = "polymarket"

    def __init__(self, *messages):
        self.inbox = list(messages)
        self.acknowledged = []

    def operator_messages(self):
        return list(self.inbox)

    def acknowledge_operator_messages(self, identifiers):
        self.acknowledged.extend(identifiers)
        self.inbox = [item for item in self.inbox if item["id"] not in set(identifiers)]


def memory_for(case) -> SessionMemory:
    memory = SessionMemory(Path(tempfile.mkdtemp()) / "session.sqlite3")
    case.addCleanup(memory.connection.close)
    return memory


CONDITION = {
    "keep": True, "kind": "fund_condition", "lasts": "until_deadline",
    "headline": "三天内用完", "instruction": "这笔 25 USDC 三天内用完，只买体育",
    "conditions": {"deadline_hours": 72, "amount": 25, "markets": ["体育"], "forbids": [],
                   "done_when": "25 USDC 花完或三天到期"},
}
NOT_AN_INSTRUCTION = {
    "keep": False, "kind": "none", "lasts": "unclear", "headline": "道谢",
    "instruction": "", "conditions": {"deadline_hours": None, "amount": None, "markets": [],
                                      "forbids": [], "done_when": ""},
}


class ReadingWhatWasWrittenTests(unittest.TestCase):
    """The words are the operator's; what they mean is the model's; neither is this code's."""

    def test_an_instruction_is_kept_in_both_forms(self) -> None:
        """The paraphrase is what gets obeyed, so the original has to survive next to it."""
        memory = memory_for(self)
        plugin = Plugin({"id": "m1", "text": "这 25U 三天内用完，只买体育", "context": {"txid": "0xabc"}})
        keeper = OperatorInstructions(memory=memory, provider=Model(CONDITION))
        kept = keeper.harvest("polymarket", plugin)
        self.assertEqual(len(kept), 1)
        stored = memory.instructions()[0]
        self.assertEqual(stored["raw_text"], "这 25U 三天内用完，只买体育")
        self.assertEqual(stored["instruction"], CONDITION["instruction"])
        self.assertEqual(stored["conditions"]["lasts"], "until_deadline")
        self.assertEqual(stored["source"], "m1")
        self.assertEqual(plugin.acknowledged, ["m1"])

    def test_a_remark_is_recorded_but_binds_nothing(self) -> None:
        """They wrote it, so it is shown; it asks for nothing, so no round is given it."""
        memory = memory_for(self)
        keeper = OperatorInstructions(memory=memory, provider=Model(NOT_AN_INSTRUCTION))
        keeper.harvest("polymarket", Plugin({"id": "m2", "text": "辛苦了", "context": {}}))
        stored = memory.instructions()
        self.assertEqual([item["kind"] for item in stored], ["remark"])
        self.assertEqual(stored[0]["status"], "noted")
        self.assertEqual(memory.active_instructions("polymarket"), [])
        self.assertEqual(keeper.payload("polymarket")["count"], 0)

    def test_a_note_the_model_could_not_read_is_still_on_the_record(self) -> None:
        """Losing it silently is indistinguishable, to the operator, from never receiving it."""
        class Broken:
            def run(self, payload, **options):
                raise RuntimeError("no model")

        memory = memory_for(self)
        keeper = OperatorInstructions(memory=memory, provider=Broken())
        keeper.harvest("polymarket", Plugin({"id": "m3", "text": "别再买长周期的", "context": {}}))
        stored = memory.instructions()[0]
        self.assertEqual(stored["raw_text"], "别再买长周期的")
        self.assertEqual(stored["kind"], "reminder")

    def test_how_long_it_lasts_is_read_off_the_note(self) -> None:
        """An expiry nobody wrote is one nobody agreed to."""
        self.assertEqual(
            READ_SCHEMA["properties"]["lasts"]["enum"],
            ["until_done", "until_deadline", "standing", "unclear"],
        )


class WhatARoundIsHandedTests(unittest.TestCase):
    """Short enough to send every round, complete enough to obey without fetching anything."""

    def _three(self, memory: SessionMemory) -> None:
        memory.record_instruction(
            platform="polymarket", source="d1", raw_text="原话" * 300, kind="reminder",
            headline="对时", instruction="记得对时", conditions={},
        )
        memory.record_instruction(
            platform="polymarket", source="d2", raw_text="原话" * 300, kind="fund_condition",
            headline="24 小时", instruction="这笔 24 小时内用完", conditions={"deadline_hours": 24},
        )
        memory.record_instruction(
            platform="polymarket", source="d3", raw_text="原话" * 300, kind="strategy_note",
            headline="只做体育", instruction="以后只做体育", conditions={"lasts": "standing"},
        )

    def test_the_rule_is_sent_whole_and_the_wording_is_not_sent_at_all(self) -> None:
        """Truncating a restriction hands the round a different instruction from the one given."""
        memory = memory_for(self)
        self._three(memory)
        payload = OperatorInstructions(memory=memory, provider=None).payload("polymarket")
        rules = [item["must"] for item in payload["open"]]
        self.assertIn("这笔 24 小时内用完", rules)
        self.assertNotIn("原话原话", json.dumps(payload, ensure_ascii=False))
        self.assertIn("READ_INSTRUCTION", payload["their_exact_words"])

    def test_what_applies_to_every_decision_comes_first(self) -> None:
        """When more are open than fit, the ones left out are the ones nobody thinks about."""
        memory = memory_for(self)
        self._three(memory)
        ordered = by_urgency(memory.active_instructions("polymarket"))
        self.assertEqual([item["kind"] for item in ordered],
                         ["strategy_note", "fund_condition", "reminder"])

    def test_the_ones_that_do_not_fit_are_counted_and_reachable(self) -> None:
        memory = memory_for(self)
        for index in range(OperatorInstructions.PROMPT_LIMIT + 3):
            memory.record_instruction(
                platform="polymarket", source=f"d{index}", raw_text="x", kind="reminder",
                headline=f"第 {index} 条", instruction=f"做第 {index} 件事", conditions={},
            )
        payload = OperatorInstructions(memory=memory, provider=None).payload("polymarket")
        self.assertEqual(len(payload["open"]), OperatorInstructions.PROMPT_LIMIT)
        self.assertEqual(payload["count"], OperatorInstructions.PROMPT_LIMIT + 3)
        self.assertEqual(payload["not_shown"], 3)
        self.assertEqual(payload["read_the_rest_with"], "LIST_INSTRUCTIONS")

    def test_the_whole_thing_is_there_for_the_round_that_asks(self) -> None:
        memory = memory_for(self)
        self._three(memory)
        detail = instruction_detail(memory, memory.instructions()[0]["id"])
        self.assertTrue(detail["operator_wrote"].startswith("原话原话"))
        self.assertIn("came_with", detail)


class ChangingTheStateOfOneTests(unittest.TestCase):
    """A round that just did the thing is the one that knows it was done."""

    def _toolset(self, memory: SessionMemory) -> MarketToolset:
        toolset = MarketToolset({"polymarket": SimpleNamespace(plugin=SimpleNamespace(name="polymarket"))},
                                "polymarket")
        toolset.create(SimpleNamespace(memory=memory, consultation=None))
        return toolset

    def test_progress_keeps_it_binding_and_done_does_not(self) -> None:
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="polymarket", source="d1", raw_text="买 5U", kind="fund_condition",
            headline="买 5U", instruction="在这个市场买 5 USDT", conditions={"amount": 5},
        )
        toolset = self._toolset(memory)
        answer = toolset.execute("NOTE_INSTRUCTION",
                                 {"id": identifier, "state": "progress", "why": "要求 5U，已买 3U，还差 2U"})
        self.assertTrue(answer["recorded"])
        self.assertTrue(answer["still_binding"])
        self.assertEqual(memory.instructions()[0]["progress"], "要求 5U，已买 3U，还差 2U")
        self.assertEqual(len(memory.active_instructions("polymarket")), 1)
        toolset.execute("NOTE_INSTRUCTION", {"id": identifier, "state": "done", "why": "5U 全部买入完成"})
        self.assertEqual(memory.active_instructions("polymarket"), [])
        self.assertEqual(memory.instructions()[0]["status"], "done")

    def test_a_state_change_without_a_reason_is_refused(self) -> None:
        """The record would otherwise say the operator was served without saying how."""
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="polymarket", source="d1", raw_text="买 5U", kind="fund_condition",
            headline="买 5U", instruction="买 5 USDT", conditions={},
        )
        toolset = self._toolset(memory)
        with self.assertRaises(ValueError):
            toolset.execute("NOTE_INSTRUCTION", {"id": identifier, "state": "done", "why": "好了"})
        self.assertEqual(memory.instructions()[0]["status"], "active")

    def test_only_what_is_open_here_can_be_touched(self) -> None:
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="binance", source="d1", raw_text="x", kind="reminder",
            headline="别的平台", instruction="别的平台的事", conditions={},
        )
        answer = self._toolset(memory).execute(
            "NOTE_INSTRUCTION", {"id": identifier, "state": "done", "why": "跟这个平台无关的理由"}
        )
        self.assertFalse(answer["recorded"])
        self.assertEqual(memory.instructions()[0]["status"], "active")

    def test_the_review_records_progress_on_the_ones_it_leaves_open(self) -> None:
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="polymarket", source="d1", raw_text="三天内用完", kind="fund_condition",
            headline="三天内用完", instruction="三天内用完这 25U",
            conditions={"deadline_hours": 72, "lasts": "until_deadline"},
        )
        keeper = OperatorInstructions(memory=memory, provider=Model(
            {"verdicts": [{"id": identifier, "state": "active", "why": "还没花完",
                           "progress": "25U 里花了 10U，还剩 15U，过了 20 小时"}]}
        ))
        closed = keeper.review("polymarket", moment="cycle_finished", evidence={})
        self.assertEqual(closed, [])
        self.assertEqual(memory.instructions()[0]["progress"], "25U 里花了 10U，还剩 15U，过了 20 小时")
        self.assertIn("progress", REVIEW_SCHEMA["properties"]["verdicts"]["items"]["required"])

    def test_the_review_carries_where_it_stood_last_time(self) -> None:
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="polymarket", source="d1", raw_text="买 5U", kind="fund_condition",
            headline="买 5U", instruction="买 5 USDT", conditions={},
        )
        memory.note_instruction_progress(identifier, "已买 3U")
        model = Model({"verdicts": []})
        OperatorInstructions(memory=memory, provider=model).review(
            "polymarket", moment="before_cycle", evidence={}
        )
        sent = model.asked[0]["payload"]["instructions"][0]
        self.assertEqual(sent["where_it_stood_last_time"], "已买 3U")


class TakingOneBackTests(unittest.TestCase):
    """Deleting is the operator withdrawing the request, which is not the robot completing it."""

    def test_a_deleted_one_reaches_no_later_round(self) -> None:
        memory = memory_for(self)
        identifier = memory.record_instruction(
            platform="polymarket", source="d1", raw_text="只买体育", kind="strategy_note",
            headline="只买体育", instruction="只买体育", conditions={},
        )
        keeper = OperatorInstructions(memory=memory, provider=None)
        self.assertEqual(keeper.payload("polymarket")["count"], 1)
        self.assertTrue(memory.forget_instruction(identifier))
        self.assertEqual(keeper.payload("polymarket")["count"], 0)
        self.assertEqual(memory.instructions(), [])
        self.assertFalse(memory.forget_instruction(identifier))


class TheOperatorsPageTests(unittest.TestCase):
    """They asked for one place that shows the words, the reading, and what became of it."""

    def _card(self):
        from prediction_market_agent.runtime.dashboard import AuditData

        root = Path(tempfile.mkdtemp())
        config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        memory = SessionMemory(config.session_db)
        identifier = memory.record_instruction(
            platform="polymarket", source="deposit:0xabc", raw_text="这 25U 三天内用完",
            kind="fund_condition", headline="三天内用完", instruction="三天内用完这 25U",
            conditions={"deadline_hours": 72, "lasts": "until_deadline"},
        )
        memory.note_instruction_progress(identifier, "花了 10U，还剩 15U")
        memory.connection.close()
        data = AuditData(config)
        self.addCleanup(data.management.shutdown)
        return data, identifier

    def test_the_page_shows_the_words_the_reading_and_the_progress(self) -> None:
        data, identifier = self._card()
        card = data.instructions()["instructions"][0]
        self.assertEqual(card["id"], identifier)
        self.assertEqual(card["operator_wrote"], "这 25U 三天内用完")
        self.assertEqual(card["instruction"], "三天内用完这 25U")
        self.assertEqual(card["came_with"], "deposit:0xabc")
        self.assertEqual(card["progress"], "花了 10U，还剩 15U")
        self.assertTrue(card["binding"])
        self.assertEqual(card["status_label"], "生效中")
        self.assertEqual(card["lasts_label"], "到期为止")
        self.assertNotIn("lasts", card["conditions"])

    def test_deleting_from_the_page_removes_it(self) -> None:
        data, identifier = self._card()
        self.assertEqual(data.forget_instruction(identifier), {"deleted": True, "id": identifier})
        self.assertEqual(data.instructions()["instructions"], [])

    def test_the_console_serves_the_panel_and_its_delete(self) -> None:
        source = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn('"/api/instructions"', source)
        self.assertIn('"/api/instructions/forget"', source)
        self.assertIn('id="instructionPanel"', source)
        self.assertIn("refreshInstructions()", source)
        for piece in ("function refreshInstructions", "async function forgetInstruction",
                      "operator-words", "/api/instructions/forget"):
            self.assertIn(piece, views)


class TheyBindBeforePreferencesTests(unittest.TestCase):
    """A preference is how the robot likes to work; this is what it was paid to do."""

    def test_the_decision_prompt_says_which_wins(self) -> None:
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        text = " ".join(BuiltInDecisionStrategy().instructions.split())
        self.assertIn("operator_instructions", text)
        self.assertIn("They outrank every preference above", text)
        self.assertIn("NOTE_INSTRUCTION", text)

    def test_the_discovery_prompt_applies_them_to_which_topics_get_a_slot(self) -> None:
        from prediction_market_agent.agent.market_discovery import BuiltInMarketDiscovery

        text = " ".join(BuiltInMarketDiscovery().instructions.split())
        self.assertIn("operator_instructions", text)
        self.assertIn("the instruction wins", text)

    def test_a_new_note_binds_the_next_decision_not_the_next_cycle(self) -> None:
        """Money is spendable the moment it arrives; a restriction read an hour later is too late."""
        source = Path("src/prediction_market_agent/runtime/engine.py").read_text()
        body = source[source.index("def _process_platform_topics"):source.index("def _catch_up_on_notes")]
        self.assertIn("self._catch_up_on_notes(runtime)", body)
        self.assertLess(body.index("self._catch_up_on_notes(runtime)"), body.index("self._evaluate_topic"))


if __name__ == "__main__":
    unittest.main()
