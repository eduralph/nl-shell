"""Which package provides a command: a lookup over the active target's local data.

The target names the data in its ``target.toml`` (``[command_provider]``, read by
``nl_shell.targets``); this module names no OS and no path (ADR-0023). The data is local:
the file is opened read-only and nothing is ever fetched (ADR-0003). When the target has
no provider, or its data is absent or unreadable, the lookup returns ``()``.

Package names come back sorted, without duplicates, and only when they pass the Debian
package-name rule (Debian Policy §5.6.1: lowercase letters, digits, ``+``, ``-`` and
``.``, at least two characters, starting with a letter or digit). The names end up in
text shown to the user, so anything else is dropped.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from nl_shell.targets import CommandProvider

# A lookup takes a command word and returns the names of the packages that provide it.
Lookup = Callable[[str], tuple[str, ...]]

# Matched against the whole string with re.fullmatch, so a trailing newline is rejected
# (a `$` anchor would accept one).
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")
# Seconds to wait on a locked database before giving up.
_TIMEOUT = 1.0


def is_package_name(name: object) -> bool:
    """True when ``name`` is a string that passes the Debian package-name rule."""
    return isinstance(name, str) and _PACKAGE_NAME.fullmatch(name) is not None


def _none(word: str) -> tuple[str, ...]:
    return ()


def provider_lookup(provider: CommandProvider | None) -> Lookup:
    """The package lookup that ``provider`` describes; ``None`` gives one that finds nothing."""
    if provider is None or provider.format != "sqlite":
        return _none

    def lookup(word: str) -> tuple[str, ...]:
        path = Path(provider.path)
        try:
            # Checked first so a lookup never creates the file.
            if not path.is_file():
                return ()
            # as_uri() percent-encodes `?`, `#` and `%` in the path, so they can't end the
            # path early or add URI parameters.
            uri = path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=_TIMEOUT)) as db:
                # The word is a bound parameter: no SQL is built from it.
                rows = db.execute(provider.query, (word,)).fetchall()
        except (sqlite3.Error, OSError, ValueError):
            return ()
        names = {row[0] for row in rows if row and is_package_name(row[0])}
        return tuple(sorted(names))

    return lookup
