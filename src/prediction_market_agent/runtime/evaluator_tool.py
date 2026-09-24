"""Expose enabled typed evaluators as an advisory Agent tool."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any


TOOL_NAME = "EVALUATE_FACTS"
_QUESTION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_INPUT_CHARS = 12_000
_MAX_IMAGE_BYTES = 16 * 1024 * 1024

DESCRIPTION = {
    "purpose": (
        "Cheap, bounded fact-based classification or scoring through an enabled evaluator. "
        "Use only after the relevant facts are already in hand, for repeated if/else-like checks "
        "or a narrow comparison that does not need new evidence or extended reasoning. "
        "Input is state (explicit facts, with source/time where relevant) plus 1-4 typed questions; "
        "output is matching answers with evaluator name and confidence. Supported types: "
        "choice selects one named criterion, score rates against an ordered list, and noul "
        "returns a 0-1 degree for a stated true/false condition. This tool does not search, read "
        "live prices, verify resolution rules, set fair value, recommend BUY/SELL or size, place "
        "orders, or replace your own final judgement. Missing or stale facts require a real read; "
        "a low-confidence answer is a reason to investigate, not a decision."
    ),
    "arguments": {
        "state": (
            "required JSON object of already verified facts; do not include secrets. An image-capable "
            "evaluator also accepts {type:'multimodal', text|value, images:[{type:'image', "
            "media_type:'image/png', data:'<base64>'}]}; remote image URLs are not accepted."
        ),
        "questions": (
            "required object of 1-4 named questions; each has type and instructions. "
            "choice has criteria {label: meaning} with 2-6 labels; score has an ordered "
            "criteria list of 2-7 anchors; noul has criteria {true: meaning, false: meaning}."
        ),
    },
    "returns": (
        "JSON object with advisory_only=true, evaluator_name, model, and answers keyed exactly "
        "like questions. Each answer repeats its type and carries confidence (0-1 or null), "
        "plus choice/probabilities, score, or noul according to the requested type. "
        "An error means no evaluator answer; continue with your own evidence-based judgement."
    ),
}


def _image_specs(state: dict[str, Any]) -> list[Any]:
    if state.get("type") == "image":
        return [state]
    if state.get("type") == "multimodal" or "image" in state or "images" in state:
        media = state.get("images")
        if media is None:
            media = [] if state.get("image") is None else [state.get("image")]
        return media if isinstance(media, list) else [media]
    return []


def _validate_images(state: dict[str, Any]) -> dict[str, Any]:
    total = 0
    specs = _image_specs(state)
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("type") != "image":
            raise ValueError("EVALUATE_FACTS images must use {type:'image', media_type, data}")
        media_type = str(spec.get("media_type") or spec.get("mime_type") or "image/png")
        if not media_type.startswith("image/"):
            raise ValueError("EVALUATE_FACTS media_type must be image/*")
        encoded = spec.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("EVALUATE_FACTS image data must be nonempty base64")
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or ";base64" not in header or not header[5:].startswith("image/"):
                raise ValueError("EVALUATE_FACTS image data URL must contain a base64 image")
        try:
            decoded = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("EVALUATE_FACTS image data is not valid base64") from error
        if not decoded:
            raise ValueError("EVALUATE_FACTS image is empty")
        total += len(decoded)
    if total > _MAX_IMAGE_BYTES:
        raise ValueError("EVALUATE_FACTS images exceed 16 MB in total")
    if not specs:
        return state

    # The ordinary fact-size limit still applies, but image bytes have their own limit above.
    def without_data(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("<base64>" if key == "data" and value.get("type") == "image"
                          else without_data(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [without_data(item) for item in value]
        return value
    return without_data(state)


def _validate(state: Any, questions: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(state, dict) or not state:
        raise ValueError("EVALUATE_FACTS state must be a nonempty facts object")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 4:
        raise ValueError("EVALUATE_FACTS needs 1-4 typed questions")
    for name, question in questions.items():
        if not isinstance(name, str) or not _QUESTION_ID.fullmatch(name):
            raise ValueError("EVALUATE_FACTS question names must be short identifiers")
        if not isinstance(question, dict):
            raise ValueError(f"EVALUATE_FACTS question {name} must be an object")
        instruction = question.get("instructions")
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 500:
            raise ValueError(f"EVALUATE_FACTS question {name} needs short instructions")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 6 or not all(
                isinstance(key, str) and key and isinstance(value, str) and value.strip()
                for key, value in criteria.items()
            ):
                raise ValueError(f"EVALUATE_FACTS choice {name} needs 2-6 named criteria")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 7 or not all(
                isinstance(item, str) and item.strip() for item in criteria
            ):
                raise ValueError(f"EVALUATE_FACTS score {name} needs 2-7 ordered anchors")
        elif kind == "noul":
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"} or not all(
                isinstance(value, str) and value.strip() for value in criteria.values()
            ):
                raise ValueError(f"EVALUATE_FACTS noul {name} needs true/false meanings")
        else:
            raise ValueError(f"EVALUATE_FACTS unsupported question type for {name}")
    sized_state = _validate_images(state)
    try:
        encoded = json.dumps(
            {"state": sized_state, "questions": questions}, ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ValueError("EVALUATE_FACTS input must be JSON-serializable") from error
    if len(encoded) > _MAX_INPUT_CHARS:
        raise ValueError("EVALUATE_FACTS input is too large; send only the relevant facts")
    return state, questions


class EvaluatorToolContribution:
    descriptions = {TOOL_NAME: DESCRIPTION}

    def __init__(self, pool: Any):
        self.pool = pool

    def create(self, _context: Any) -> "EvaluatorToolContribution":
        return self

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != TOOL_NAME:
            raise KeyError(name)
        if not isinstance(arguments, dict):
            raise ValueError("EVALUATE_FACTS arguments must be an object")
        state, questions = _validate(arguments.get("state"), arguments.get("questions"))
        result = self.pool.answer_questions(state, questions)
        return {"ok": True, "advisory_only": True, **result}
