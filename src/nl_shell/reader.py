"""The quarantined reader: a failed command's raw output in, a validated diagnosis out.

ADR-0017 and spec ``docs/design/specs/reader-schema/v1.md``. The raw text of a failed
command (its stdout and stderr) is untrusted: an attacker may have written any of it. The
reader sends it to the model in one request of its own (its own prompt, constrained
decoding with ``SCHEMA``, no tools), then checks the answer:

- it must parse as one JSON object that matches ``SCHEMA`` exactly (spec §1, §3). Any
  parse error, extra property, wrong type, unknown ``category``, more than three
  ``missing`` items, or a ``missing`` / ``unit`` value that fails its pattern rejects the
  whole answer: the result is ``NoDiagnosis``. Nothing is repaired (ADR-0017 §3), as in
  ``nl_shell.proposal``;
- each ``missing`` name is kept only if it is a package in the local dpkg/apt index or a
  command an indexed package provides; ``unit`` only if it is in
  ``systemctl list-unit-files`` (spec §2). The caller passes those checks in
  (``Lookups``); none of them reads ``$PATH``, which the attacker can influence. A name
  that fails them is dropped, and the diagnosis still stands.

A ``Diagnosis`` holds only the ``category`` and the kept names. The text shown to the user
(``Diagnosis.message``) and the state-block item (``Diagnosis.state_item``, labelled
``untrusted``, ADR-0016) are built from nl-shell's own templates keyed on ``category``
(spec §4, §5); there is no free-text field. ``SCHEMA`` is the spec's schema, kept here
once: the validator reads its enum, limits and patterns from it, and the request sends the
same object for constrained decoding (``RESPONSE_SCHEMA``).

The reader's prompt is target data, ``<target folder>/prompts/reader.yaml``, kept apart
from the command model's ``prompts.yaml``. Its keys, both required:

    system            the reader's system prompt: a non-empty string
    request_template  the request: a string holding `{stdout}` once, then `{stderr}` once
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nl_shell import command_provider, render
from nl_shell.backend import Backend, BackendError, ResponseSchema
from nl_shell.prompts import PROMPTS_DIR, Message

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category"],
    "properties": {
        "category": {
            "enum": [
                "not-found",
                "permission",
                "syntax",
                "missing-package",
                "missing-file",
                "service-failed",
                "network",
                "disk-full",
                "busy-or-locked",
                "unknown",
            ]
        },
        "missing": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9.+@_-]{0,62}$"},
        },
        "unit": {
            "type": "string",
            "pattern": "^[A-Za-z0-9@._-]{1,128}\\.(service|socket|timer|mount|target)$",
        },
    },
}
SCHEMA_NAME = "reader_v1"
RESPONSE_SCHEMA = ResponseSchema(SCHEMA_NAME, SCHEMA)

_PROPERTIES = SCHEMA["properties"]
CATEGORIES = tuple(_PROPERTIES["category"]["enum"])
MISSING_MAX = _PROPERTIES["missing"]["maxItems"]
# Matched with re.fullmatch, so a trailing newline is rejected (the spec's `$` alone
# would accept one).
_MISSING = re.compile(_PROPERTIES["missing"]["items"]["pattern"])
_UNIT = re.compile(_PROPERTIES["unit"]["pattern"])

# The taint label of everything the reader returns (ADR-0016: reader output is untrusted).
UNTRUSTED = "untrusted"

READER_PROMPT_NAME = "reader.yaml"
READER_KEYS = ("system", "request_template")
STDOUT = "{stdout}"
STDERR = "{stderr}"

# The user-facing text per category (spec §4). nl-shell's words, never the reader's.
TEMPLATES: dict[str, str] = {
    "not-found": "The command or a file it named was not found.",
    "permission": "The command was refused permission.",
    "syntax": "The command line has a syntax error.",
    "missing-package": "The command needs a package that isn't installed.",
    "missing-file": "The command needs a file or directory that doesn't exist.",
    "service-failed": "A system service failed.",
    "network": "The command couldn't reach the network.",
    "disk-full": "The disk is full.",
    "busy-or-locked": "Something the command needs is busy or locked.",
    "unknown": "The cause of the failure isn't clear.",
}
MISSING_LINE = "Missing: {names}."
UNIT_LINE = "Unit: {unit}."
NO_DIAGNOSIS = "No diagnosis for that failure."


class ReaderPromptError(Exception):
    """The reader's prompt file could not be used; the message names the file."""


@dataclass(frozen=True)
class ReaderPrompt:
    system: str
    request_template: str


@dataclass(frozen=True)
class Lookups:
    """The local, read-only checks a name must pass to be kept (spec §2).

    ``package_exists``: is this a package in the dpkg/apt index. ``command_package``: the
    indexed packages that provide this command (``command_provider.provider_lookup``
    gives one). ``unit_exists``: is this unit in ``systemctl list-unit-files``.
    """

    package_exists: Callable[[str], bool]
    command_package: command_provider.Lookup
    unit_exists: Callable[[str], bool]


@dataclass(frozen=True)
class StateItem:
    """One state-block item (#11 builds the block): its taint label and its text."""

    label: str
    text: str


@dataclass(frozen=True)
class Diagnosis:
    """A validated answer: the category and the names that exist on this system."""

    category: str
    missing: tuple[str, ...] = ()
    unit: str | None = None
    label: str = UNTRUSTED

    def message(self) -> str:
        """The text for the user, from the category's template, escaped."""
        lines = [TEMPLATES[self.category]]
        if self.missing:
            lines.append(MISSING_LINE.format(names=", ".join(self.missing)))
        if self.unit is not None:
            lines.append(UNIT_LINE.format(unit=self.unit))
        return render.escape(" ".join(lines))

    def state_item(self) -> StateItem:
        """What the command model may see of this diagnosis, labelled ``untrusted``."""
        parts = [f"last failure: {self.category}"]
        if self.missing:
            parts.append("missing " + " ".join(self.missing))
        if self.unit is not None:
            parts.append(f"unit {self.unit}")
        return StateItem(label=self.label, text=render.escape("; ".join(parts)))


@dataclass(frozen=True)
class NoDiagnosis:
    """No usable answer. ``why`` is a fixed phrase for the log; it holds no answer text."""

    why: str

    def message(self) -> str:
        return NO_DIAGNOSIS

    def state_item(self) -> None:
        return None


Result = Diagnosis | NoDiagnosis


class _Reject(Exception):
    pass


# --- the prompt --------------------------------------------------------------------------


def prompt_path(target_folder: Path) -> Path:
    return target_folder / PROMPTS_DIR / READER_PROMPT_NAME


def load_prompt(target_folder: Path) -> ReaderPrompt:
    """The active target's reader prompt (required, strict, PyYAML's safe loader only)."""
    path = prompt_path(target_folder)
    try:
        text = path.read_bytes().decode("utf-8")
        doc = yaml.safe_load(text)
    except FileNotFoundError:
        raise ReaderPromptError(f"{path}: missing") from None
    except OSError as exc:
        raise ReaderPromptError(f"{path}: cannot read: {exc.strerror or exc}") from None
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ReaderPromptError(f"{path}: not valid YAML: {type(exc).__name__}") from None
    if not isinstance(doc, dict) or sorted(doc, key=str) != sorted(READER_KEYS):
        raise ReaderPromptError(
            f"{path}: must be a mapping with exactly `system` and `request_template`"
        )
    system, template = doc["system"], doc["request_template"]
    if not isinstance(system, str) or not system.strip():
        raise ReaderPromptError(f"{path}: `system` must be a non-empty string")
    if (
        not isinstance(template, str)
        or template.count(STDOUT) != 1
        or template.count(STDERR) != 1
        or template.index(STDOUT) > template.index(STDERR)
    ):
        raise ReaderPromptError(
            f"{path}: `request_template` must hold {STDOUT} once, then {STDERR} once"
        )
    return ReaderPrompt(system=system, request_template=template)


def build(prompt: ReaderPrompt, stdout: str, stderr: str) -> tuple[Message, ...]:
    """The reader's model input. Plain splicing, so braces in the output stay as they are."""
    head, _, rest = prompt.request_template.partition(STDOUT)
    middle, _, tail = rest.partition(STDERR)
    request = head + stdout + middle + stderr + tail
    return (Message("system", prompt.system), Message("user", request))


# --- the request and the check -----------------------------------------------------------


def diagnose(
    backend: Backend, prompt: ReaderPrompt, stdout: str, stderr: str, lookups: Lookups
) -> Result:
    """One reader request for a failed command's output, and its validated answer."""
    messages = build(prompt, stdout, stderr)
    try:
        raw = backend.complete(messages, schema=RESPONSE_SCHEMA)
    except BackendError:
        return NoDiagnosis("no answer from the model")
    return validate(raw, lookups)


def validate(raw: str, lookups: Lookups) -> Result:
    """Check one reader answer strictly: a ``Diagnosis`` or a ``NoDiagnosis``."""
    try:
        return _validate(raw, lookups)
    except _Reject as exc:
        return NoDiagnosis(str(exc))


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Reject("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise _Reject("non-JSON number")


def _validate(raw: str, lookups: Lookups) -> Diagnosis:
    # Every _Reject text is fixed: none carries a value from the answer.
    if not isinstance(raw, str):
        raise _Reject("response is not text")
    try:
        doc = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise _Reject("not valid JSON") from None
    if not isinstance(doc, dict):
        raise _Reject("not a JSON object")
    if set(doc) - set(_PROPERTIES):
        raise _Reject("extra properties")
    for key in SCHEMA["required"]:
        if key not in doc:
            raise _Reject(f"missing {key}")
    category = doc["category"]
    if not isinstance(category, str) or category not in CATEGORIES:
        raise _Reject("unknown category")
    names: Sequence[str] = ()
    if "missing" in doc:
        names = doc["missing"]
        if not isinstance(names, list):
            raise _Reject("missing is not a list")
        if len(names) > MISSING_MAX:
            raise _Reject("too many missing items")
        for name in names:
            if not isinstance(name, str) or _MISSING.fullmatch(name) is None:
                raise _Reject("a missing item fails its pattern")
    unit = doc.get("unit")
    if "unit" in doc and (not isinstance(unit, str) or _UNIT.fullmatch(unit) is None):
        raise _Reject("unit fails its pattern")
    # Spec §2: names that don't exist on this system are dropped, not rejected.
    kept: list[str] = []
    for name in names:
        if name not in kept and _exists(name, lookups):
            kept.append(name)
    if unit is not None and not lookups.unit_exists(unit):
        unit = None
    return Diagnosis(category=category, missing=tuple(kept), unit=unit)


def _exists(name: str, lookups: Lookups) -> bool:
    """A package in the index, or a command an indexed package provides. Never ``$PATH``."""
    if lookups.package_exists(name):
        return True
    return any(command_provider.is_package_name(p) for p in lookups.command_package(name))
