"""Reading os-release(5): the OS identity that target selection matches on.

The file is ``/etc/os-release``; ``/usr/lib/os-release`` is read only when the first does
not exist (os-release(5): "Applications should check for the former, and exclusively use
its data if it exists, and only fall back to the latter if it is missing"). Any other
failure to read or decode the first file is an error, never a fallback.

Lines are shell-style ``KEY=value`` assignments. A value may be unquoted or enclosed in
double or single quotes; inside double quotes a backslash escapes ``"``, ``\\``, ``$``
and the backtick. Blank lines and lines starting with ``#`` are ignored, and so is a line
that is not a valid assignment (as systemd does). Variable expansion is not performed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

ETC_OS_RELEASE = Path("/etc/os-release")
USR_LIB_OS_RELEASE = Path("/usr/lib/os-release")
DEFAULT_PATHS: tuple[Path, ...] = (ETC_OS_RELEASE, USR_LIB_OS_RELEASE)

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class OsReleaseError(Exception):
    """os-release could not be read; the message says which file and why."""


def parse(text: str) -> dict[str, str]:
    """The ``KEY=value`` pairs of os-release text, quotes removed."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or not _KEY.fullmatch(key):
            continue
        value = _unquote(raw)
        if value is not None:
            fields[key] = value
    return fields


def _unquote(raw: str) -> str | None:
    """One shell word with its quoting removed, or None if ``raw`` is not one word."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        char = raw[i]
        if char == "'":
            end = raw.find("'", i + 1)
            if end < 0:
                return None  # unbalanced quote
            out.append(raw[i + 1:end])
            i = end + 1
        elif char == '"':
            i += 1
            while True:
                if i >= n:
                    return None  # unbalanced quote
                char = raw[i]
                if char == '"':
                    i += 1
                    break
                if char == "\\" and i + 1 < n and raw[i + 1] in '"\\$`':
                    out.append(raw[i + 1])
                    i += 2
                    continue
                out.append(char)
                i += 1
        elif char == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
        elif char.isspace():
            return None  # unquoted whitespace: not a single word
        else:
            out.append(char)
            i += 1
    return "".join(out)


def read(paths: Sequence[Path] = DEFAULT_PATHS) -> dict[str, str]:
    """The fields of the first os-release file in ``paths`` that exists.

    A file that does not exist moves on to the next path; any other read or decode error
    raises OsReleaseError at once.
    """
    for path in paths:
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OsReleaseError(f"cannot read {path}: {exc.strerror or exc}") from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise OsReleaseError(f"cannot read {path}: not valid UTF-8") from None
        return parse(text)
    looked = ", ".join(str(path) for path in paths)
    raise OsReleaseError(f"no os-release file found (looked for {looked})")
