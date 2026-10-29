"""The policy gate's shared data types (spec ``policy-gate/v2``).

One ``Invocation`` per simple command is what the command layer (``rules``), live-session
resolution (``live``) and the sensitive-path check (``sensitive``) consume, whether it came
from the read-only grammar (``grammar``, the only source that can lead to ``read-only``)
or from the advisory tree-sitter walk (``parse``, which can only raise a class).
"""

from __future__ import annotations

from dataclasses import dataclass

CLASSES = ("read-only", "mutating", "session-altering", "destructive", "privileged")
RANK = {
    "read-only": 0,
    "mutating": 1,
    "session-altering": 1,
    "destructive": 2,
    "privileged": 3,
}


@dataclass(frozen=True)
class Stdin:
    """Where a simple command's standard input comes from.

    ``kind`` is one of:

    - ``terminal``: first in its pipeline or standing alone, with no redirect of fd 0;
    - ``pipe``: a later stage of a pipeline, or the command inside ``>(…)``;
    - ``file``: ``<`` from a literal path; ``value`` is the path;
    - ``literal``: a here-string whose word is literal, or a here-doc whose delimiter is
      quoted (or unquoted with no ``$``, `` ` `` or ``\\`` in its body); ``value`` is the
      text;
    - ``unknown``: everything else.

    Consumers MUST treat ``unknown`` as the worst case for each rule: as a pipe for the
    sudo-pipe rule and as the terminal for the terminal-read rule.
    """

    kind: str
    value: str | None = None


TERMINAL = Stdin("terminal")
PIPE = Stdin("pipe")
UNKNOWN = Stdin("unknown")


@dataclass(frozen=True)
class Word:
    """One shell word: ``text`` after quote removal, ``literal`` when bash would expand
    nothing in it, ``source`` as written."""

    text: str
    literal: bool
    source: str


@dataclass(frozen=True)
class Assignment:
    """One assignment. ``role`` is ``prefix`` (``VAR=x cmd``), ``declaration`` (inside
    ``export``/``declare``/…) or ``standalone``."""

    name: str
    value: Word | None
    role: str


@dataclass(frozen=True)
class Redirect:
    """One redirection."""

    kind: str  # file_redirect | heredoc_redirect | herestring_redirect | loose
    descriptor: str | None
    operator: str
    target: Word | None
    output: bool  # writes somewhere (an "output redirection")
    exempt: bool  # one of the exempt forms: >/dev/null, 2>&1, 1>&2, >&2
    device_socket: bool  # /dev/tcp or /dev/udp
    feed: Stdin | None  # the stdin a here-doc/here-string gives
    display: str  # operator and target as written (to be escaped before display)


@dataclass(frozen=True)
class Invocation:
    """One simple command."""

    kind: str  # command | declaration | unset
    words: tuple[Word, ...]
    assignments: tuple[Assignment, ...]
    redirects: tuple[Redirect, ...]
    stdin: Stdin
    in_function: bool = False

    @property
    def command_word(self) -> Word | None:
        return self.words[0] if self.words else None
