"""Putting a question back to the model, inside the round that is already running.

A plugin occasionally reaches a fork that is not its to settle. Two instructions arrive that cannot
both be carried out, and which one survives is a statement about intent rather than a matter of
policy - intent that lives in the model that issued them, not in the plugin receiving them. The
plugin could pick a rule, newest wins or oldest wins, and be right most of the time and silently
wrong the rest; asking costs one round trip and is right by construction.

So the plugin asks, in its own words, and the answer comes back as data. Two things make that safe
enough to act on. The question is spliced into the session already in progress, so the model answers
with the same market, the same trace and the same strategy text in front of it rather than as a
stranger reading a fragment. And the shape of the reply is fixed by a schema built from the options
the plugin declared, so "choose 1, 2, 3 or 4" is enforced by the contract the backend must satisfy
rather than hoped for from wording. Nothing here decides what the options are or what any of them
mean - that is the plugin's, and the framework would be guessing.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

LOGGER = logging.getLogger(__name__)

CONSULT_MISSION = (
    "A plugin you just called cannot continue until you settle one thing. Answer the question "
    "below by choosing exactly one of the numbered options. values_json must encode a JSON object "
    "holding the fields that option asks for, and '{}' when it asks for none. The reason is read "
    "by whoever sees the outcome, so say what you meant, not what you chose."
)

MAX_ATTEMPTS = 2
"""How many times the model may be asked before the question is abandoned.

A schema can say "an integer from this set" but not "this particular option also needs a figure
attached", so a first reply can miss a field that only becomes required once a choice is made, and
saying what was wrong usually fixes it. Beyond that the model is not converging, and looping inside a tool call would
spend the round's budget on a question instead of on the trade.
"""

_TYPES = {"number": "number", "integer": "integer", "string": "string", "boolean": "boolean"}


@dataclass(frozen=True)
class ConsultOption:
    """One answer the plugin will accept, numbered so the reply can be a value rather than prose.

    `fields` is what this particular option cannot be acted on without. An option that means "do it
    differently" is not yet an instruction until the difference is stated, and a plugin receiving
    the choice alone would have to ask again. Each entry reads "<type>: <what it means>", the same
    way tool arguments are described, and the type is what the reply is checked against.
    """

    value: int
    label: str
    fields: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label, "fields": dict(self.fields)}


@dataclass(frozen=True)
class ConsultAnswer:
    """What the model said, or why nothing usable came back.

    A caller part-way through a consequential act needs one of two clear states, never a third that
    looks like an answer. `ok` false means no choice was obtained - the model was unreachable, or
    never produced a reply matching the options - and the caller must fall back to something it can
    defend on its own rather than treating a default as consent.
    """

    ok: bool
    choice: int = 0
    label: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    error: str = ""

    def chose(self, value: int) -> bool:
        return self.ok and self.choice == int(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "choice": self.choice,
            "label": self.label,
            "values": dict(self.values),
            "reason": self.reason,
            "error": self.error,
        }


class AgentConsult(Protocol):
    """The asking interface, as a plugin receives it."""

    def ask(
        self, question: str, options: Sequence[ConsultOption], *, subject: str = ""
    ) -> ConsultAnswer: ...


def answer_schema(options: Sequence[ConsultOption]) -> dict[str, Any]:
    """The reply contract: a choice drawn from these options, its payload, and why.

    The enum is the enforcement. A model told in prose to answer 1-4 can still answer "option two";
    one handed a schema whose choice is an integer from a closed set cannot, and the backend rejects
    the reply before the framework ever sees it.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "choice": {"type": "integer", "enum": [int(option.value) for option in options]},
            "values_json": {"type": "string", "maxLength": 2000},
            "reason": {"type": "string", "minLength": 1, "maxLength": 600},
        },
        "required": ["choice", "values_json", "reason"],
    }


def _coerce(spec: str, value: Any) -> Any:
    kind = _TYPES.get(str(spec).split(":")[0].strip().lower())
    if kind is None:
        raise ValueError(f"declares an unknown field type {spec!r}")
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        raise ValueError("must be true or false")
    if kind == "string":
        text = str(value).strip()
        if not text:
            raise ValueError("must not be empty")
        return text
    if isinstance(value, bool):
        raise ValueError("must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return int(number) if kind == "integer" else number


def read_values(option: ConsultOption, raw: Any) -> dict[str, Any]:
    """Check the payload against what this option declared it needs, and keep only that.

    Only declared fields survive. Anything else the model attached is noise the plugin never asked
    for, and refusing the whole reply over noise would spend an attempt without learning anything.
    """
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    if not isinstance(raw, dict):
        raise ValueError("values_json must encode a JSON object")
    values: dict[str, Any] = {}
    for name, spec in option.fields.items():
        if name not in raw or raw[name] is None:
            raise ValueError(f"option {option.value} also needs {name} ({spec})")
        try:
            values[name] = _coerce(spec, raw[name])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} {error}") from error
    return values


def _validated(options: Sequence[ConsultOption]) -> tuple[str, dict[int, ConsultOption]]:
    """Reject a malformed option list here, where it is the plugin's bug and not the model's."""
    if not options:
        return "no options were offered, so there is nothing the model could answer", {}
    by_value: dict[int, ConsultOption] = {}
    for option in options:
        if int(option.value) in by_value:
            return f"option value {option.value} was offered twice", {}
        if not str(option.label).strip():
            return f"option {option.value} has no label, so its meaning is unstated", {}
        for name, spec in option.fields.items():
            if _TYPES.get(str(spec).split(":")[0].strip().lower()) is None:
                return f"option {option.value} field {name} declares an unknown type {spec!r}", {}
        by_value[int(option.value)] = option
    return "", by_value


class AgentConsultation:
    """The live session, handed round so anything the model called can call back into it.

    Only the provider can ask the model anything: it holds the backend and the running trace, and a
    replacement is chosen per attempt when one provider fails over to another. So the provider binds
    this on entry and clears it on exit, and everything downstream holds the same object without
    needing to know which backend is behind it or whether one is there at all.
    """

    def __init__(self) -> None:
        self._backend: Any = None
        self._payload: Mapping[str, Any] = {}
        self._trace: list[dict[str, Any]] = []
        self._preamble = ""
        self._instructions = ""
        self._recorder: Any = None
        self._provider = ""
        self._in_flight: dict[str, Any] | None = None

    @property
    def open(self) -> bool:
        return self._backend is not None

    def bind(
        self,
        *,
        backend: Any,
        payload: Mapping[str, Any],
        trace: list[dict[str, Any]],
        preamble: str,
        instructions: str,
        recorder: Any = None,
        provider: str = "",
    ) -> None:
        self._backend = backend
        self._payload = payload
        self._trace = trace
        self._preamble = preamble
        self._instructions = instructions
        self._recorder = recorder
        self._provider = provider

    def entering(self, tool: str, arguments: Mapping[str, Any]) -> None:
        """Note the call now running, because a question from inside it is about that call.

        A tool only joins the trace once it has returned, so a plugin that asks something part-way
        through its own invocation would otherwise be questioning a session that does not yet
        mention the request being questioned. There is no answering "which of these two asks should
        stand" without seeing the one just made.
        """
        self._in_flight = {"tool": str(tool), "arguments": dict(arguments)}

    def left(self) -> None:
        self._in_flight = None

    def release(self) -> None:
        self._backend = None
        self._recorder = None
        self._in_flight = None

    def _prompt(
        self, question: str, options: Sequence[ConsultOption], rejected: str, subject: str
    ) -> str:
        return (
            f"{self._preamble}\n\n{CONSULT_MISSION}"
            + self._instructions
            + (f"\n\nASKED_BY:\n{subject}" if subject else "")
            + (
                "\n\nCALL_IN_PROGRESS (your own call, which is what this question is about):\n"
                + json.dumps(self._in_flight, ensure_ascii=False, sort_keys=True, default=str)
                if self._in_flight
                else ""
            )
            + "\n\nQUESTION:\n"
            + question
            + "\n\nANSWER_OPTIONS:\n"
            + json.dumps([option.to_dict() for option in options], ensure_ascii=False)
            + (f"\n\nPREVIOUS_ATTEMPT_REJECTED:\n{rejected}" if rejected else "")
            + "\n\nMARKET_INPUT:\n"
            + json.dumps(self._payload, ensure_ascii=False, sort_keys=True, default=str)
            + "\n\nRESEARCH_TRACE:\n"
            + json.dumps(self._trace, ensure_ascii=False, sort_keys=True, default=str)
        )

    def _record(self, **values: Any) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder(step_index=len(self._trace), **values)
        except Exception as error:  # recording must never break the thing it records
            LOGGER.warning("could not record a consultation: %s", error)

    def _failed(self, subject: str, question: str, error: str) -> ConsultAnswer:
        LOGGER.warning("consultation for %s produced no answer: %s", subject or "a plugin", error)
        self._record(
            input_payload={"consultation": subject, "question": question},
            raw_output="",
            control=None,
            tool_name=subject,
            status="CONSULT_ERROR",
            error=error,
        )
        return ConsultAnswer(ok=False, error=error)

    def ask(
        self, question: str, options: Sequence[ConsultOption], *, subject: str = ""
    ) -> ConsultAnswer:
        """Ask, validate, and hand back either one of these options or an explained failure.

        Never raises. The caller is part-way through an operation it has already begun, and an
        exception surfacing here would abandon that half-done rather than let the caller choose how
        to cope with not having got an answer.
        """
        options = list(options)
        problem, by_value = _validated(options)
        if problem:
            return self._failed(subject, question, f"The options are unusable: {problem}")
        if not self.open:
            return self._failed(
                subject, question, "No model session is open, so there is nobody to ask"
            )
        schema = answer_schema(options)
        rejected = ""
        for attempt in range(MAX_ATTEMPTS):
            prompt = self._prompt(question, options, rejected, subject)
            try:
                response = self._backend.complete(prompt, schema, "plugin_consultation")
            except Exception as error:
                return self._failed(subject, question, f"The model could not be reached: {error}")
            reply = response.value if isinstance(response.value, dict) else {}
            option = by_value.get(int(reply.get("choice", 0) or 0))
            if option is None:
                rejected = f"{reply.get('choice')!r} is not one of the offered option values"
            else:
                try:
                    values = read_values(option, reply.get("values_json", "{}"))
                except (ValueError, json.JSONDecodeError) as error:
                    rejected = str(error)
                else:
                    answer = ConsultAnswer(
                        ok=True,
                        choice=option.value,
                        label=option.label,
                        values=values,
                        reason=str(reply.get("reason", "")).strip(),
                    )
                    self._record(
                        input_payload={
                            "consultation": subject,
                            "question": question,
                            "options": [item.to_dict() for item in options],
                        },
                        raw_output=response.raw_output,
                        control=reply,
                        tool_name=subject,
                        result=answer.to_dict(),
                        status="CONSULT_OK",
                    )
                    # The session has to carry this forward, or the next step is taken by a model
                    # that does not know it was asked and will walk into the same fork again.
                    self._trace.append(
                        {
                            "consulted_by": subject,
                            "question": question,
                            "your_answer": answer.to_dict(),
                        }
                    )
                    return answer
            LOGGER.info("consultation attempt %s rejected: %s", attempt + 1, rejected)
        return self._failed(
            subject, question, f"The model did not answer within the options: {rejected}"
        )
