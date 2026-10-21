"""OS targets: loading ``targets/<id>/target.toml`` and choosing the active target.

ADR-0023 §Decision. A target is one supported OS release, and everything OS-specific about
it lives in its folder; code names no OS. This module reads only ``target.toml``: the other
files of a target folder (``allowlist.toml``, ``sensitive.toml``, ...) are loaded by the
issues that use them, and none is required here.

Choosing the target ("Choosing the target"): the target whose os-release ``ID`` and
``VERSION_ID`` match exactly, with no ``ID_LIKE`` or nearby-version fallback; or the target
the user names (``--target`` or ``target =`` in config.toml). A named target that does not
match this OS is still used, and the selection records that it does not match, with the
reason spec ``policy-gate/v1`` §7.7 gives. The gate (#12) reads that state.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nl_shell import os_release

# The repository's targets/ folder, next to src/. Not packaged yet (#48, #50).
TARGETS_ROOT = Path(__file__).resolve().parents[2] / "targets"
TARGET_FILE = "target.toml"
SUPPORTED_SHELLS = ("bash",)  # ADR-0023 "Shells later"
UNKNOWN = "unknown"
MISMATCH_REASON = "target does not match this OS"
COMMAND_PROVIDER_FORMATS = ("sqlite",)  # [command_provider] format

# A target id is a plain folder name: no separators, no `.` or `..`, nothing hidden.
_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class TargetError(Exception):
    """A target could not be loaded or chosen; the message says why."""


class NoMatchingTarget(TargetError):
    """No target was named and none matches this OS exactly."""

    def __init__(
        self,
        detected_id: str,
        detected_version_id: str,
        available: Sequence[str],
        cause: str | None = None,
    ) -> None:
        self.detected_id = detected_id
        self.detected_version_id = detected_version_id
        self.available = tuple(available)
        self.cause = cause
        if cause is not None:
            text = f"cannot tell which OS this is: {cause}"
        else:
            detected = f"{detected_id} {detected_version_id}"
            text = f"no target matches this OS (detected: {detected})"
        super().__init__(text)


@dataclass(frozen=True)
class CommandProvider:
    """``[command_provider]``: local data that names the package providing a command.

    ``format`` is the kind of source (only ``sqlite``), ``path`` the absolute path of the
    file, opened read-only and never fetched (ADR-0003), and ``query`` one SQL statement
    with a single ``?`` for the command word that returns package names in its first
    column.
    """

    format: str
    path: str
    query: str


@dataclass(frozen=True)
class Target:
    """One loaded ``target.toml``. ``fields`` is the whole parsed file, read-only."""

    id: str
    display_name: str
    os_release_id: str
    os_release_version_id: str
    shell: str
    package_owner_query: tuple[str, ...]
    sudo_implementation: str
    sudo_version: str
    folder: Path
    fields: Mapping[str, Any] = field(repr=False, compare=False)
    # Optional: a target without it names no package for "command not found".
    command_provider: CommandProvider | None = None

    def matches(self, release: Mapping[str, str]) -> bool:
        """True when os-release ``ID`` and ``VERSION_ID`` equal this target's, exactly."""
        return (
            release.get("ID") == self.os_release_id
            and release.get("VERSION_ID") == self.os_release_version_id
        )


@dataclass(frozen=True)
class Selection:
    """The active target for one run, and whether it matches this OS.

    ``detected_id`` / ``detected_version_id`` are os-release's values, or ``"unknown"`` when
    os-release could not be read or lacks the field. ``reason`` is None when the target
    matches and ``MISMATCH_REASON`` when it does not.
    """

    target: Target
    matches: bool
    detected_id: str
    detected_version_id: str
    reason: str | None

    def status_lines(self) -> tuple[str, ...]:
        """The ``:status`` lines for this selection."""
        if self.matches:
            state = "target matches this OS"
        else:
            state = (
                f"target does not match this OS "
                f"(detected: {self.detected_id} {self.detected_version_id})"
            )
        return (f"target: {self.target.id} ({self.target.display_name})", state)


def is_plain_id(target_id: str) -> bool:
    return bool(_PLAIN_ID.fullmatch(target_id)) and target_id not in (".", "..")


def available(root: Path = TARGETS_ROOT) -> list[str]:
    """The ids of the target folders under ``root``, sorted."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(e.name for e in entries if e.is_dir() and is_plain_id(e.name))


def load(target_id: str, root: Path = TARGETS_ROOT) -> Target:
    """Load and validate ``root/<target_id>/target.toml``."""
    if not is_plain_id(target_id):
        raise TargetError(
            f"invalid target id {target_id!r}: a target id is a plain folder name "
            "(letters, digits, '.', '_' and '-'; no '/')"
        )
    folder = root / target_id
    if not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve()):
        names = ", ".join(available(root)) or "none"
        raise TargetError(f"no target {target_id!r} in {root} (available: {names})")
    path = folder / TARGET_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise TargetError(f"{path}: missing") from None
    except OSError as exc:
        raise TargetError(f"{path}: cannot read: {exc.strerror or exc}") from None
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise TargetError(f"{path}: not valid TOML: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise TargetError(f"{path}: not valid TOML: {exc}") from None
    return _validate(doc, folder, path)


def _validate(doc: dict[str, Any], folder: Path, path: Path) -> Target:
    def string(table: Mapping[str, Any], key: str, name: str) -> str:
        if key not in table:
            raise TargetError(f"{path}: missing required field `{name}`")
        value = table[key]
        if not isinstance(value, str) or not value:
            raise TargetError(f"{path}: `{name}` must be a non-empty string")
        return value

    def table(key: str) -> Mapping[str, Any]:
        if key not in doc:
            raise TargetError(f"{path}: missing required table `[{key}]`")
        value = doc[key]
        if not isinstance(value, dict):
            raise TargetError(f"{path}: `{key}` must be a table")
        return value

    target_id = string(doc, "id", "id")
    if target_id != folder.name:
        raise TargetError(
            f"{path}: id {target_id!r} does not match its folder name {folder.name!r}"
        )
    display_name = string(doc, "display_name", "display_name")
    shell = string(doc, "shell", "shell")
    if shell not in SUPPORTED_SHELLS:
        raise TargetError(
            f"{path}: shell {shell!r} is not supported "
            f"(supported: {', '.join(SUPPORTED_SHELLS)})"
        )
    match = table("os_release")
    sudo = table("sudo")
    if "package_owner_query" not in doc:
        raise TargetError(f"{path}: missing required field `package_owner_query`")
    query = doc["package_owner_query"]
    if (
        not isinstance(query, list)
        or not query
        or not all(isinstance(word, str) and word for word in query)
    ):
        raise TargetError(
            f"{path}: `package_owner_query` must be a non-empty list of non-empty strings"
        )
    # Optional, unlike [os_release] and [sudo]: absent means no provider.
    provider = None
    if "command_provider" in doc:
        source = table("command_provider")
        kind = string(source, "format", "command_provider.format")
        if kind not in COMMAND_PROVIDER_FORMATS:
            raise TargetError(
                f"{path}: command_provider.format {kind!r} is not supported "
                f"(supported: {', '.join(COMMAND_PROVIDER_FORMATS)})"
            )
        data_path = string(source, "path", "command_provider.path")
        if not Path(data_path).is_absolute():
            raise TargetError(f"{path}: `command_provider.path` must be an absolute path")
        sql = string(source, "query", "command_provider.query")
        if sql.count("?") != 1:
            raise TargetError(
                f"{path}: `command_provider.query` must have exactly one `?` "
                "(the command word)"
            )
        provider = CommandProvider(format=kind, path=data_path, query=sql)
    return Target(
        id=target_id,
        display_name=display_name,
        os_release_id=string(match, "ID", "os_release.ID"),
        os_release_version_id=string(match, "VERSION_ID", "os_release.VERSION_ID"),
        shell=shell,
        package_owner_query=tuple(query),
        sudo_implementation=string(sudo, "implementation", "sudo.implementation"),
        sudo_version=string(sudo, "version", "sudo.version"),
        folder=folder,
        fields=MappingProxyType(doc),
        command_provider=provider,
    )


def select(
    named: str | None,
    root: Path = TARGETS_ROOT,
    os_release_paths: Sequence[Path] = os_release.DEFAULT_PATHS,
) -> Selection:
    """Choose the active target for this run.

    ``named`` is the target the user named (flag over config, resolved by the caller), or
    None. Raises TargetError when the named target cannot be loaded, and NoMatchingTarget
    when nothing is named and no target matches this OS exactly.
    """
    try:
        release: Mapping[str, str] | None = os_release.read(os_release_paths)
        cause = None
    except os_release.OsReleaseError as exc:
        release, cause = None, str(exc)
    detected_id = (release or {}).get("ID") or UNKNOWN
    detected_version_id = (release or {}).get("VERSION_ID") or UNKNOWN

    if named is not None:
        target = load(named, root)
        matches = release is not None and target.matches(release)
        return Selection(
            target=target,
            matches=matches,
            detected_id=detected_id,
            detected_version_id=detected_version_id,
            reason=None if matches else MISMATCH_REASON,
        )

    if release is None:
        raise NoMatchingTarget(UNKNOWN, UNKNOWN, available(root), cause)
    # The exact match can only be the folder named `<ID>-<VERSION_ID>` (ADR-0023); its
    # target.toml must agree with os-release too, or it is not a match.
    candidate = f"{release.get('ID', '')}-{release.get('VERSION_ID', '')}"
    if (
        release.get("ID")
        and release.get("VERSION_ID")
        and is_plain_id(candidate)
        and (root / candidate).is_dir()
    ):
        target = load(candidate, root)
        if target.matches(release):
            return Selection(
                target=target,
                matches=True,
                detected_id=detected_id,
                detected_version_id=detected_version_id,
                reason=None,
            )
    raise NoMatchingTarget(detected_id, detected_version_id, available(root))
