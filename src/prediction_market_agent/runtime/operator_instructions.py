"""What the operator said when they paid, kept and applied until it stops applying.

Money arrives with words attached. "Use it within three days", "only sports for this one", "stop
buying the long-dated stuff", "this is the last of it" - ordinary things anybody says when handing
over money, and all of them conditions on what happens next. They used to reach exactly one round,
as a note on a funding answer, and then vanish: the round that read them was the only round that
ever knew about them.

So a note is read once by a model, which says what it is - a condition on the money, a change to how
to trade, something to keep in mind, or nothing to keep at all - and what survives is written down.
From then on every decision and every survey is given the open ones, and twice a cycle the model is
asked which of them are finished. A finished one stops being asked about but stays on the record,
because the operator's question later is not "what is open" but "did you do what I asked".

Nothing here interprets the words itself. Which of these a note is, and whether it has been carried
out, are judgements about language and about what the robot did - and the only thing here that can
make either is the model.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)

READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "keep": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["fund_condition", "strategy_note", "reminder", "none"]},
        # How long it lasts is part of what the note says, not something this runtime decides for
        # it: "use this within three days" ends when it is used or the days run out, "stop buying
        # the long-dated stuff" holds until somebody says otherwise, and a note that does not say
        # is left open rather than quietly given an expiry nobody asked for.
        "lasts": {"type": "string", "enum": ["until_done", "until_deadline", "standing", "unclear"]},
        "headline": {"type": "string", "maxLength": 80},
        "instruction": {"type": "string", "maxLength": 400},
        "conditions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "deadline_hours": {"type": ["number", "null"], "minimum": 0},
                "amount": {"type": ["number", "null"], "minimum": 0},
                "markets": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 80}},
                "forbids": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 120}},
                "done_when": {"type": "string", "maxLength": 200},
            },
            "required": ["deadline_hours", "amount", "markets", "forbids", "done_when"],
        },
    },
    "required": ["keep", "kind", "lasts", "headline", "instruction", "conditions"],
}

READ_MISSION = (
    "Somebody just put money into this robot's account and wrote this note with it. Say what it is. "
    "A note can be a condition on that money (a deadline to use it, what it may or may not be spent "
    "on, how big a position it allows), a change to how the robot should trade from now on, "
    "something to keep in mind, or none of those - a thank-you, a greeting, an empty string. Keep "
    "only what the robot could actually obey or check later; `keep` false for the rest, and do not "
    "invent a condition the note does not state. `instruction` is what the robot must do or avoid, "
    "in one or two plain sentences addressed to it. `lasts` is how long it holds, read off the note "
    "itself: until the thing asked for is done, until a deadline it states, standing until somebody "
    "says otherwise, or unclear - and unclear is the honest answer when the note does not say, "
    "because an expiry nobody wrote is one nobody agreed to. `done_when` is the observable thing "
    "that would mean this is finished; leave it empty for a standing rule. Fill only the condition "
    "fields the note actually gives; use null or an empty list for the rest."
)

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer"},
                    "state": {"type": "string", "enum": ["active", "done", "expired"]},
                    "why": {"type": "string", "maxLength": 200},
                    # Asked for on every verdict, including the ones that stay open, because "still
                    # active" is not an answer to "how is it going" - and the operator's question,
                    # three days after handing over money for something, is the second one.
                    "progress": {"type": "string", "maxLength": 300},
                },
                "required": ["id", "state", "why", "progress"],
            },
        },
    },
    "required": ["verdicts"],
}

REVIEW_MISSION = (
    "These are things the operator asked for when they funded this robot, and this is what the "
    "robot has done since. Each one carries how long it was meant to last, taken from the note "
    "itself. One that lasts `until_done` closes when the thing asked for has actually happened - "
    "the money spent as required, the trade made - not when the robot intends to. One that lasts "
    "`until_deadline` closes when that time has passed, done or not; say which. One that is "
    "`standing` does not close because it was obeyed once: a rule about how to trade holds until "
    "the operator lifts it, and a single trade that followed it is not the end of it. One marked "
    "`unclear` stays open unless it has plainly become impossible. When in doubt it is still open: "
    "closing one early means the robot stops obeying something the operator is still expecting. "
    "Say why in a few words, in the operator's own terms, because they will read it. And for every "
    "one of them, open or closed, say where it has got to in the numbers it was written in - what "
    "was asked, what has happened, what is left: \"5 USDT asked for, 3 bought, 2 to go\", \"of the "
    "72 hours, 40 have passed\". `progress` is that line; it is not a verdict and not a plan, and "
    "if the record does not show any movement, say that instead of inventing some."
)


class OperatorInstructions:
    """Reads notes into standing instructions, applies them, and closes them when they are done."""

    def __init__(self, *, memory: SessionMemory, provider: Any) -> None:
        self.memory = memory
        self.provider = provider

    def harvest(self, platform: str, plugin: Any) -> list[int]:
        """Take whatever the operator has said to a plugin and make instructions of it.

        The plugin collects the words - it owns the panel they were typed into - and hands them
        over exactly as written. What they mean is not its business, and what to do about them is
        not the model's to decide alone either: it says what the note is, and only notes that can
        actually be obeyed or checked are kept.
        """
        messages = self._messages(plugin)
        if not messages:
            return []
        kept: list[int] = []
        handled: list[str] = []
        for message in messages:
            text = str(message.get("text", "")).strip()
            handled.append(str(message.get("id", "")))
            if not text:
                continue
            try:
                answer = self.provider.run(
                    {
                        "note": text,
                        "written_when": message.get("context", {}),
                        "platform": platform,
                    },
                    schema=READ_SCHEMA,
                    schema_name="operator_note",
                    mission=READ_MISSION,
                    instructions="",
                    max_tool_steps=0,
                )
            except Exception as error:
                # A note nobody could read is not a note nobody wrote: keep it as a reminder so the
                # operator can see it was received, and let the next review decide what to do.
                LOGGER.warning("could not read an operator note: %s", error)
                kept.append(self.memory.record_instruction(
                    platform=platform, source=str(message.get("id", "")), raw_text=text,
                    kind="reminder", headline=text[:60],
                    instruction="这条附言没能被模型解读，原文照录", conditions={},
                ))
                continue
            value = answer.value if hasattr(answer, "value") else answer
            if not value.get("keep") or str(value.get("kind", "none")) == "none":
                # Binds nothing, but is not thrown away: the operator wrote it, and a note that
                # vanishes because a model judged it idle is indistinguishable, from their side,
                # from one that was never received. Kept as `noted`, shown, never handed to a round.
                self.memory.record_instruction(
                    platform=platform, source=str(message.get("id", "")), raw_text=text,
                    kind="remark", headline=str(value.get("headline", "")) [:80] or text[:60],
                    instruction="", conditions={"lasts": "none"}, status="noted",
                )
                LOGGER.info("operator note recorded as a remark, binding nothing: %s", text[:80])
                continue
            kept.append(self.memory.record_instruction(
                platform=platform,
                source=str(message.get("id", "")),
                raw_text=text,
                kind=str(value.get("kind", "reminder")),
                headline=str(value.get("headline", ""))[:80],
                instruction=str(value.get("instruction", ""))[:400],
                conditions={**(value.get("conditions") or {}), "lasts": value.get("lasts", "unclear")},
            ))
        self._acknowledge(plugin, handled)
        return kept

    def review(self, platform: str, *, moment: str, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        """Ask which of the open instructions have been carried out, and close those.

        Asked at the two moments a cycle can be judged from: before it starts, and after it has
        finished and before the robot waits. Nothing is asked when nothing is open, because the one
        thing worse than forgetting what the operator said is spending a model call every round to
        be told again that nothing has changed.
        """
        open_items = self.memory.active_instructions(platform)[: self.REVIEW_LIMIT]
        if not open_items:
            return []
        try:
            answer = self.provider.run(
                {
                    "moment": moment,
                    "instructions": [
                        {
                            "id": item["id"], "kind": item["kind"], "asked": item["instruction"],
                            "in_their_words": str(item["raw_text"])[:400],
                            "lasts": item["conditions"].get("lasts", "unclear"),
                            "where_it_stood_last_time": item.get("progress", ""),
                            "conditions": {k: v for k, v in item["conditions"].items() if k != "lasts"},
                            "asked_at_ms": item["created_at"],
                        }
                        for item in open_items
                    ],
                    "now_ms": int(time.time() * 1000),
                    "what_has_happened": evidence,
                },
                schema=REVIEW_SCHEMA,
                schema_name="operator_instruction_review",
                mission=REVIEW_MISSION,
                instructions="",
                max_tool_steps=0,
            )
        except Exception as error:
            LOGGER.warning("the operator's instructions could not be reviewed: %s", error)
            return []
        value = answer.value if hasattr(answer, "value") else answer
        known = {item["id"] for item in open_items}
        closed: list[dict[str, Any]] = []
        for verdict in value.get("verdicts", []) or []:
            identifier = int(verdict.get("id", 0))
            state = str(verdict.get("state", "active"))
            if identifier not in known:
                continue
            progress = str(verdict.get("progress", "")).strip()
            if progress:
                self.memory.note_instruction_progress(identifier, progress)
            if state == "active":
                continue
            self.memory.resolve_instruction(
                identifier, status=state, resolution=str(verdict.get("why", ""))
            )
            closed.append({"id": identifier, "state": state, "why": verdict.get("why", "")})
        self.memory.note_instruction_check(sorted(known))
        return closed

    def payload(self, platform: str) -> dict[str, Any]:
        """The open instructions as a round is given them: a table of contents, not the file.

        These accumulate. Every deposit can add one, they stay until they are carried out, and the
        text that comes with money is not written to a length limit - so pasting all of them, in
        full, into every decision and every survey is a prompt that grows without bound and a bill
        that grows with it, mostly for words no round will use.

        What a round needs in front of it is the obeyable line: what it must do, where that stood
        last time, when it runs out. The operator's exact wording is evidence - it decides arguments
        about whether the line is faithful - and evidence is fetched when there is an argument, with
        READ_INSTRUCTION. Everything here is short and complete on its own, or marked as trimmed so
        the round knows there is more to read before acting on it.
        """
        items = by_urgency(self.memory.active_instructions(platform))
        catalogue = [self.catalogue_entry(item) for item in items[:self.PROMPT_LIMIT]]
        payload: dict[str, Any] = {"open": catalogue, "count": len(items)}
        if len(items) > len(catalogue):
            payload["not_shown"] = len(items) - len(catalogue)
            payload["read_the_rest_with"] = "LIST_INSTRUCTIONS"
        payload["their_exact_words"] = "READ_INSTRUCTION(id) - not included above, fetch when it matters"
        return payload

    REVIEW_LIMIT = 20
    """How many are judged in one review, matching what the schema can answer for."""

    PROMPT_LIMIT = 8
    """How many open instructions a round is shown before the rest have to be asked for.

    Not a limit on how many bind - all of them do - but on how many arrive unasked. Past this the
    round is told how many more there are and how to read them, which costs one tool call in the
    rare cycle that needs it instead of a longer prompt in every cycle that does not.
    """

    @staticmethod
    def catalogue_entry(item: dict[str, Any]) -> dict[str, Any]:
        """One line of the table of contents, carrying the whole of what has to be obeyed.

        The rule itself is never shortened. A round that reads "only spend this on" and has to
        fetch the rest has already been handed a different instruction from the one the operator
        gave, and it will act on it at exactly the moment the restriction was supposed to bite.
        What the model wrote here is one or two sentences by construction; what is left out is the
        operator's own wording, which is evidence about the rule rather than the rule.
        """
        conditions = {k: v for k, v in (item.get("conditions") or {}).items() if v not in (None, [], "")}
        entry: dict[str, Any] = {
            "id": item["id"],
            "kind": item["kind"],
            "must": str(item.get("instruction", "")),
            "conditions": conditions,
            "asked_at_ms": item["created_at"],
        }
        if item.get("progress"):
            entry["where_it_stands"] = str(item["progress"])[:180]
        return entry

    def full(self, identifier: int) -> dict[str, Any]:
        """One instruction whole, for the round that needs to check the line against the words."""
        return instruction_detail(self.memory, identifier)

    @staticmethod
    def _messages(plugin: Any) -> list[dict[str, Any]]:
        reader = getattr(plugin, "operator_messages", None)
        if not callable(reader):
            return []
        try:
            messages = reader()
        except Exception as error:
            LOGGER.warning("%s could not hand over what the operator said: %s", plugin, error)
            return []
        return [item for item in (messages or []) if isinstance(item, dict)]

    @staticmethod
    def _acknowledge(plugin: Any, identifiers: list[str]) -> None:
        acknowledge = getattr(plugin, "acknowledge_operator_messages", None)
        if not callable(acknowledge) or not identifiers:
            return
        try:
            acknowledge(identifiers)
        except Exception as error:
            LOGGER.warning("%s would not take the acknowledgement: %s", plugin, error)


def by_urgency(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the open instructions by how badly a round needs to see them.

    Only matters when there are more than fit, and then it matters a lot: the ones left out of the
    catalogue are the ones a round will not think about. A standing change to how to trade comes
    first because it applies to every decision this round will make. Then conditions on money,
    soonest deadline first, because those expire whether or not anyone got to them. Reminders last:
    they are true, and nothing goes wrong in the next hour if one waits.
    """
    rank = {"strategy_note": 0, "fund_condition": 1, "reminder": 2}

    def key(item: dict[str, Any]) -> tuple[int, float, int]:
        conditions = item.get("conditions") or {}
        deadline = conditions.get("deadline_hours")
        elapsed_hours = (time.time() * 1000 - float(item.get("created_at", 0))) / 3_600_000
        left = float(deadline) - elapsed_hours if deadline not in (None, "") else float("inf")
        return (rank.get(str(item.get("kind")), 3), left, int(item.get("created_at", 0)))

    return sorted(items, key=key)


def instruction_detail(memory: SessionMemory, identifier: int) -> dict[str, Any]:
    """One instruction whole - the words, the conditions, the transfer it came with.

    The catalogue a round is handed carries the obeyable line and nothing else. This is the rest of
    it, fetched when the line is not enough: when the wording decides the argument, when the
    operator has to be quoted, when what came with the money matters.
    """
    for item in memory.instructions(limit=500):
        if int(item["id"]) == int(identifier):
            return {
                "id": item["id"],
                "kind": item["kind"],
                "status": item["status"],
                "must": item["instruction"],
                "operator_wrote": item["raw_text"],
                "conditions": item["conditions"],
                "where_it_stands": item["progress"],
                "closed_because": item["resolution"],
                "came_with": item["source"],
                "asked_at_ms": item["created_at"],
            }
    return {}


def evidence_for_review(memory: SessionMemory, platform: str, *, limit: int = 12) -> dict[str, Any]:
    """What the robot has done lately, which is what a verdict about "done" has to be read off."""
    try:
        return {"recent_decisions": memory.decisions_since_instruction(platform, limit=limit)}
    except Exception:
        LOGGER.exception("could not read what the robot has done for the instruction review")
        return {"recent_decisions": []}
