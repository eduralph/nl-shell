"""Loading a target's gate conformance vectors, ``targets/<id>/gate-vectors.toml``.

Spec ``policy-gate/v2`` Conformance: each target has its own vectors. The file is a list
of ``[[vector]]`` tables with these keys, and only these:

- ``command`` (string, required);
- ``origin`` (``"proposal"`` or ``"typed"``, required);
- ``rejected`` (bool, required);
- ``class`` (one of the five classes): required when ``rejected = false``, absent when
  ``rejected = true`` (a rejected row asserts the rejection only);
- ``reason`` (string, optional): a substring some reason must contain;
- ``label`` (one of the five classes, optional): the model's label;
- ``row`` (int, optional): the spec's conformance row number.

The loader follows ``nl_shell.targets.load``: it validates everything and raises
``VectorError`` naming the file and the entry.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nl_shell import targets
from nl_shell.gate.types import CLASSES

VECTORS_FILE = "gate-vectors.toml"
ORIGINS = ("proposal", "typed")
_KEYS = frozenset(("command", "origin", "rejected", "class", "reason", "label", "row"))


class VectorError(Exception):
    """The vectors file could not be loaded; the message names the entry."""


@dataclass(frozen=True)
class Vector:
    command: str
    origin: str
    rejected: bool
    cls: str | None
    reason: str | None
    label: str | None
    row: int | None


def load_target(target_id: str, root: Path = targets.TARGETS_ROOT) -> tuple[Vector, ...]:
    """Load ``root/<target_id>/gate-vectors.toml``."""
    if not targets.is_plain_id(target_id):
        raise VectorError(f"invalid target id {target_id!r}")
    return load(root / target_id / VECTORS_FILE)


def load(path: Path) -> tuple[Vector, ...]:
    """Load and validate one vectors file."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise VectorError(f"{path}: missing") from None
    except OSError as exc:
        raise VectorError(f"{path}: cannot read: {exc.strerror or exc}") from None
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise VectorError(f"{path}: not valid TOML: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise VectorError(f"{path}: not valid TOML: {exc}") from None
    extra = sorted(set(doc) - {"vector"})
    if extra:
        raise VectorError(f"{path}: unknown top-level key `{extra[0]}`")
    entries = doc.get("vector", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise VectorError(f"{path}: `vector` must be a list of [[vector]] tables")
    return tuple(
        _validate(entry, f"{path}: vector {n}") for n, entry in enumerate(entries, 1)
    )


def _validate(entry: dict[str, Any], where: str) -> Vector:
    if "row" in entry and isinstance(entry["row"], int):
        where = f"{where} (row {entry['row']})"
    unknown = sorted(set(entry) - _KEYS)
    if unknown:
        raise VectorError(f"{where}: unknown key `{unknown[0]}`")
    for key in ("command", "origin", "rejected"):
        if key not in entry:
            raise VectorError(f"{where}: missing required key `{key}`")
    command = entry["command"]
    if not isinstance(command, str) or not command:
        raise VectorError(f"{where}: `command` must be a non-empty string")
    origin = entry["origin"]
    if origin not in ORIGINS:
        raise VectorError(f"{where}: `origin` must be one of {', '.join(ORIGINS)}")
    rejected = entry["rejected"]
    if not isinstance(rejected, bool):
        raise VectorError(f"{where}: `rejected` must be a boolean")
    cls = entry.get("class")
    if rejected and cls is not None:
        raise VectorError(f"{where}: `class` must be absent on a rejected vector")
    if not rejected and cls is None:
        raise VectorError(f"{where}: missing `class` (required when not rejected)")
    if cls is not None and cls not in CLASSES:
        raise VectorError(f"{where}: `class` must be one of {', '.join(CLASSES)}")
    label = entry.get("label")
    if label is not None and label not in CLASSES:
        raise VectorError(f"{where}: `label` must be one of {', '.join(CLASSES)}")
    reason = entry.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise VectorError(f"{where}: `reason` must be a non-empty string")
    row = entry.get("row")
    if row is not None and (not isinstance(row, int) or isinstance(row, bool) or row < 1):
        raise VectorError(f"{where}: `row` must be a positive integer")
    return Vector(
        command=command,
        origin=origin,
        rejected=rejected,
        cls=cls,
        reason=reason,
        label=label,
        row=row,
    )
