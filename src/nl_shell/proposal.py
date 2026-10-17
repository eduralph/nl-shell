"""The strict ``proposal/v1`` parser (spec ``docs/design/specs/proposal/v1.md``).

ADR-0018 §3 and spec rule 5: a response that fails to parse, fails the schema or breaks
rule 2 is rejected, never repaired. A ``command`` holding a hidden character
(``nl_shell.render.is_hidden``) is rejected too, so the REPL never shows or runs text the
user cannot see. ``SCHEMA`` is the spec's schema, kept here once: the parser reads its
enums and lengths from it, and #77 sends the same object for constrained decoding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nl_shell import render

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"enum": ["command", "clarify", "refuse"]},
        "command": {"type": "string", "minLength": 1, "maxLength": 2000},
        "risk": {
            "enum": [
                "read-only",
                "mutating",
                "session-altering",
                "destructive",
                "privileged",
            ]
        },
        "reason": {
            "enum": [
                "unclear-request",
                "not-a-shell-task",
                "unsafe-request",
                "missing-information",
            ]
        },
    },
}

_PROPERTIES = SCHEMA["properties"]
KINDS = tuple(_PROPERTIES["kind"]["enum"])
RISKS = tuple(_PROPERTIES["risk"]["enum"])
REASONS = tuple(_PROPERTIES["reason"]["enum"])
COMMAND_MIN = _PROPERTIES["command"]["minLength"]
COMMAND_MAX = _PROPERTIES["command"]["maxLength"]


@dataclass(frozen=True)
class Command:
    """``kind = "command"``: one bash command line (may hold LF) and its stated risk."""

    command: str
    risk: str


@dataclass(frozen=True)
class Answer:
    """``kind = "clarify"`` or ``"refuse"``, with its reason."""

    kind: str
    reason: str


@dataclass(frozen=True)
class Rejected:
    """The response was not a usable proposal. ``why`` is for the log, never repaired."""

    why: str


Proposal = Command | Answer


class _Reject(Exception):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Reject("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise _Reject(f"non-JSON number {name}")


def parse(raw: str) -> Proposal | Rejected:
    """Parse one model response strictly: a ``Proposal`` or a ``Rejected``."""
    try:
        return _parse(raw)
    except _Reject as exc:
        return Rejected(str(exc))


def _parse(raw: str) -> Proposal:
    if not isinstance(raw, str):
        raise _Reject("response is not text")
    try:
        doc = json.loads(
            raw, object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, RecursionError) as exc:
        # ValueError covers invalid JSON and an integer over Python's digit limit.
        raise _Reject(f"not valid JSON: {type(exc).__name__}") from None
    if not isinstance(doc, dict):
        raise _Reject("not a JSON object")
    extra = sorted(set(doc) - set(_PROPERTIES))
    if extra:
        raise _Reject(f"extra properties: {len(extra)}")
    for key in SCHEMA["required"]:
        if key not in doc:
            raise _Reject(f"missing {key}")
    kind = _enum(doc, "kind", KINDS)
    if "risk" in doc:
        _enum(doc, "risk", RISKS)
    if "reason" in doc:
        _enum(doc, "reason", REASONS)
    if "command" in doc:
        command = doc["command"]
        if not isinstance(command, str):
            raise _Reject("command is not a string")
        if not COMMAND_MIN <= len(command) <= COMMAND_MAX:
            raise _Reject(f"command length {len(command)} is out of range")
        if render.has_hidden(command):
            raise _Reject("command holds a hidden character")
    # Rule 2.
    if kind == "command":
        if "command" not in doc or "risk" not in doc:
            raise _Reject("command without command or risk")
        if "reason" in doc:
            raise _Reject("command with reason")
        return Command(command=doc["command"], risk=doc["risk"])
    if "reason" not in doc:
        raise _Reject(f"{kind} without reason")
    if "command" in doc:
        raise _Reject(f"{kind} with command")
    return Answer(kind=kind, reason=doc["reason"])


def _enum(doc: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = doc[key]
    if not isinstance(value, str) or value not in allowed:
        raise _Reject(f"unknown {key}")
    return value
