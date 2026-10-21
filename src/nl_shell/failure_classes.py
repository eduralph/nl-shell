"""Failure classes by exit code: the deterministic first pass over a failed command (#155).

``classify`` looks only at trusted facts: the ``session.CommandResult`` built from the
control channel's done record (ADR-0014 Decision 2) and the command text the user
confirmed. It never reads the command's output, so nothing in it can be steered by what
the command printed (ADR-0017: only the reader sees raw untrusted text). Every message is
fixed text; no model is involved.

Order, for a command that ran: status 0 is no failure; 130 is an interrupt (the user's
Ctrl-C, not an error); a command whose text could make bash run a privilege program is
PRIVILEGE (no correction, no reader: ADR-0015 / ADR-0006); then 127 not found, 126 not
executable, 129-192 killed by a signal (``bash(1)`` EXIT STATUS: 128 + n), and the rest
is OTHER. A done record without a start record means bash read the input but ran nothing
(pty-protocol v1 §4.1): blank or comment-only text is no failure, privileged text is
PRIVILEGE, anything else is a syntax error (OTHER).

The privilege check is text-only and stateless until #12's parse: regex and character
tests over the raw text, with no quote or nesting tracking. It treats a command as
privileged when (P1) a privilege program's name appears anywhere once backslashes and
quote characters are deleted, (P2) the text holds a code-running or quoting marker,
(P3) the first word of any segment is not a plain name, (P4) a word names a shell
function, or (P5) a word names an alias whose value is privileged. Over-matching is the
safe direction. The known misses are listed in the threat model's "Known gaps".

``to_reader`` means only "eligible by exit status": dropping secret-labelled output
(ADR-0025) before the reader is #61's job, not this module's. The package suggestion for
127 comes from an injected ``lookup``; the package provider (#156) owns the data.
"""

from __future__ import annotations

import dataclasses
import enum
import itertools
import os
import re
import shlex
import signal
from collections.abc import Callable, Iterable

from nl_shell.router import BASH_BUILTINS, first_token
from nl_shell.session import CommandResult

Lookup = Callable[[str], Iterable[str]]


class FailureClass(enum.Enum):
    NONE = "none"
    INTERRUPTED = "interrupted"
    PRIVILEGE = "privilege"
    NOT_FOUND = "not-found"
    NOT_EXECUTABLE = "not-executable"
    SIGNAL = "signal"
    OTHER = "other"


@dataclasses.dataclass(frozen=True)
class Failure:
    kind: FailureClass
    status: int
    pipestatus: tuple[int, ...]
    message: str  # fixed text, "" for NONE
    offer_correction: bool
    to_reader: bool  # eligible by exit status only (see the module docstring)
    is_error: bool
    command_word: str | None = None  # NOT_FOUND: the word, when it is safe to show
    packages: tuple[str, ...] = ()  # NOT_FOUND: packages that provide the word
    signal_number: int | None = None
    signal_name: str | None = None


PRIVILEGE_MESSAGE = (
    "This command may have used elevated privileges, so nl-shell offers no correction "
    "and does not read its output."
)
INTERRUPTED_MESSAGE = "Interrupted."
NOT_EXECUTABLE_MESSAGE = "The command was found but could not be run (permission or noexec)."
OOM_HINT = " A SIGKILL is often the kernel's out-of-memory killer."
SYNTAX_MESSAGE = "Bash read the command but ran nothing; the text may have a syntax error."

# The `compgen -k` list of bash 5, kept beside router.BASH_BUILTINS.
BASH_KEYWORDS = frozenset(
    """
    if then else elif fi case esac for select while until do done in function time
    { } ! [[ ]] coproc
    """.split()
)

PRIVILEGE_NAMES = (
    "sudo su pkexec doas run0 machinectl sudo-rs sudo.ws sudoedit sudoedit-rs".split()
)
MAX_ALIAS_DEPTH = 8
MAX_SHOWN = 64  # longest command word or package name shown in a message
MAX_PACKAGES = 5
MAX_LOOKUP_ITEMS = 1000  # bound on how much of a lookup's result is read

# P1: the longest names first, so `sudo-rs` is tried before `sudo`.
_NAME = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(n) for n in sorted(PRIVILEGE_NAMES, key=len, reverse=True))
    + r")(?![A-Za-z0-9_.\-])"
)
# P2: markers that run code or change quoting.
_MARKERS = ("$(", "`", "<(", ">(", "$'", '$"', "\\")
# P3: where a segment ends (quotes are ignored on purpose).
_CUT = re.compile(r"[;&|()\n]")
_SKIP_WORDS = frozenset("if then else elif do while until ! time { coproc".split())
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_REDIRECTION = re.compile(r"[0-9]*(?:<<<|<<|>>|<>|>\||>&|<&|&>>|&>|<|>)")
_PLAIN = re.compile(r"(?:~/)?[A-Za-z0-9._+/-]+")
# Rule 4: text that is not one simple command gets no package suggestion.
_NOT_SIMPLE = frozenset(";&|()<>$`\\\n")
# `alias -p` prints `alias -- NAME=VALUE` for a name that starts with `-`.
_ALIAS_START = re.compile(r"alias (?:-- )?([^=\s]+)=")
_ALIAS_LINE = re.compile(r"^alias (?:-- )?([^=\s]+)=", re.MULTILINE)
_DISABLED_LINE = re.compile(r"^enable -n (\S+)", re.MULTILINE)


def classify(result: CommandResult, command: str, lookup: Lookup | None = None) -> Failure:
    """Classify one command from its ``CommandResult`` and the confirmed ``command`` text.

    ``lookup(word)`` returns the packages that provide ``word``; it is asked only for a
    127 whose word bash really did not find, and a ``None`` result or an exception means
    no suggestion. Raises ``TypeError`` for anything but a ``CommandResult``.
    """
    if not isinstance(result, CommandResult):
        raise TypeError(f"classify needs a session.CommandResult, not {type(result).__name__}")
    if not isinstance(command, str):
        raise TypeError("command must be str")
    status = result.status
    pipestatus = tuple(result.pipestatus)

    def make(kind: FailureClass, message: str, **extra) -> Failure:
        return Failure(
            kind=kind,
            status=status,
            pipestatus=pipestatus,
            message=message,
            offer_correction=kind
            not in (FailureClass.NONE, FailureClass.INTERRUPTED, FailureClass.PRIVILEGE),
            to_reader=kind is FailureClass.OTHER,
            is_error=kind not in (FailureClass.NONE, FailureClass.INTERRUPTED),
            **extra,
        )

    snapshot = result.snapshot
    if not result.ran:
        if _blank_or_comment(command):
            return make(FailureClass.NONE, "")
        if is_privileged(command, snapshot.functions, snapshot.aliases):
            return make(FailureClass.PRIVILEGE, PRIVILEGE_MESSAGE)
        return make(FailureClass.OTHER, SYNTAX_MESSAGE)
    if status == 0:
        return make(FailureClass.NONE, "")
    if status == 130:
        return make(FailureClass.INTERRUPTED, INTERRUPTED_MESSAGE)
    if is_privileged(command, snapshot.functions, snapshot.aliases):
        return make(FailureClass.PRIVILEGE, PRIVILEGE_MESSAGE)
    if status == 127:
        word = _unresolved_word(command, snapshot)
        if word is None:
            return make(FailureClass.NOT_FOUND, "Command not found.")
        packages = _packages(lookup, word)
        message = f"Command not found: {word}."
        if len(packages) == 1:
            message += f" It is provided by the package {packages[0]}."
        elif packages:
            message += f" It is provided by the packages {', '.join(packages)}."
        return make(FailureClass.NOT_FOUND, message, command_word=word, packages=packages)
    if status == 126:
        return make(FailureClass.NOT_EXECUTABLE, NOT_EXECUTABLE_MESSAGE)
    if 129 <= status <= 192:
        number = status - 128
        name = _signal_name(number)
        message = f"The command was stopped by signal {number} ({name})."
        if number == signal.SIGKILL:
            message += OOM_HINT
        return make(FailureClass.SIGNAL, message, signal_number=number, signal_name=name)
    return make(FailureClass.OTHER, f"The command failed with exit status {status}.")


# --- the privilege check (P1-P5) ---------------------------------------------------------


def is_privileged(text: str, functions: Iterable[str] = (), aliases: str = "") -> bool:
    """True when ``text`` could make bash run a privilege program, by P1-P5."""
    alias_values = _parse_aliases(aliases)
    verdict, _ = _privileged(text, frozenset(functions), alias_values, {}, set(), 0, None)
    return verdict


def _privileged(
    text: str,
    functions: frozenset[str],
    alias_values: dict[str, str | None],
    memo: dict[str, int],
    open_names: set[str],
    depth: int,
    own_name: str | None,
) -> tuple[bool, int]:
    """(privileged, height): height is how many alias levels the text's words reach."""
    if _name_anywhere(text) or _has_marker(text) or _unclear_first_word(text):
        return True, 0
    pieces = _pieces(text.replace("'", "").replace('"', ""))
    if functions and any(piece in functions for piece in pieces):
        return True, 0
    height = 0
    for piece in pieces:
        # Bash does not expand an alias inside its own value (`alias ls='ls --color=auto'`),
        # so an alias's own name in its value is not followed; P1-P4 still see the value.
        if piece == own_name or piece not in alias_values:
            continue
        verdict, below = _alias_privileged(
            piece, functions, alias_values, memo, open_names, depth + 1
        )
        if verdict:
            return True, 0
        height = max(height, below)
    return False, height


def _alias_privileged(
    name: str,
    functions: frozenset[str],
    alias_values: dict[str, str | None],
    memo: dict[str, int],
    open_names: set[str],
    depth: int,
) -> tuple[bool, int]:
    """P5: the alias's value is privileged. A cycle, a chain deeper than MAX_ALIAS_DEPTH,
    or a value the parse could not pin down (None) counts as privileged.

    Each value is checked at most once per call. Only "not privileged" is saved, with the
    height of the alias chain below it, so a later visit at a greater depth still applies
    the depth cut-off; a "privileged" answer ends the whole call, so it needs no saving.
    """
    if name in open_names or depth > MAX_ALIAS_DEPTH:
        return True, 0
    if name in memo:
        height = memo[name]
        return depth + height - 1 > MAX_ALIAS_DEPTH, height
    value = alias_values[name]
    if value is None:
        return True, 0
    open_names.add(name)
    verdict, below = _privileged(value, functions, alias_values, memo, open_names, depth, name)
    open_names.discard(name)
    if verdict:
        return True, 0
    memo[name] = below + 1
    return False, below + 1


def _name_anywhere(text: str) -> bool:
    """P1: a privilege program's name, after deleting backslash-newline, then \\ ' "."""
    flat = text.replace("\\\n", "")
    flat = flat.replace("\\", "").replace("'", "").replace('"', "")
    return _NAME.search(flat) is not None


def _has_marker(text: str) -> bool:
    """P2: a substitution, backtick, process substitution, ANSI-C or locale quote, or a
    backslash anywhere."""
    return any(marker in text for marker in _MARKERS)


def _unclear_first_word(text: str) -> bool:
    """P3: some segment's first word is not a builtin, a keyword or a plain name."""
    for segment in _CUT.split(text):
        word = _first_word(segment.split())
        if word is not None and not _plain(word):
            return True
    return False


def _first_word(pieces: list[str]) -> str | None:
    skip_next = False
    for piece in pieces:
        if skip_next:
            skip_next = False
            continue
        if piece in _SKIP_WORDS or _ASSIGNMENT.match(piece):
            continue
        operator = _REDIRECTION.match(piece)
        if operator:
            skip_next = operator.end() == len(piece)  # `> out`: the next piece is the file
            continue
        return piece
    return None


def _plain(word: str) -> bool:
    return word in BASH_BUILTINS or word in BASH_KEYWORDS or _PLAIN.fullmatch(word) is not None


def _pieces(text: str) -> list[str]:
    """Every whitespace-separated piece of every segment (cut as in P3)."""
    return [piece for segment in _CUT.split(text) for piece in segment.split()]


def _parse_aliases(text: str) -> dict[str, str | None]:
    """``alias -p`` text as {name: value}. None marks a value that cannot be pinned down,
    which P5 treats as privileged.

    Each value is read by its quoting (``'...'`` runs and backslash escapes, as bash
    prints them), so a quoted value may run over several lines and a line inside it that
    starts ``alias NAME=`` is part of the value, not a new alias. Bash never prints a name
    twice, so a repeated name is None. An unclosed quote takes the rest of the text as
    written, and every name that starts a line in that rest is None too.
    """
    values: dict[str, str | None] = {}
    pos, size = 0, len(text)
    while pos < size:
        match = _ALIAS_START.match(text, pos)
        if match is None:
            pos = _next_line(text, pos)
            continue
        end, closed = _word_end(text, match.end())
        raw = text[match.end() : end]
        try:
            value: str | None = " ".join(shlex.split(raw))
        except ValueError:
            value = raw
        name = match.group(1)
        values[name] = None if name in values else value
        if not closed:
            for inner in _ALIAS_LINE.finditer(text, match.end()):
                values[inner.group(1)] = None
            break
        pos = _next_line(text, end)
    return values


def _word_end(text: str, start: int) -> tuple[int, bool]:
    """Where the shell word at ``start`` ends, and whether its quotes were closed."""
    i, size = start, len(text)
    while i < size and text[i] not in " \t\n":
        if text[i] == "'":
            close = text.find("'", i + 1)
            if close < 0:
                return size, False
            i = close + 1
        elif text[i] == "\\":
            i += 2
        else:
            i += 1
    return min(i, size), True


def _next_line(text: str, pos: int) -> int:
    newline = text.find("\n", pos)
    return len(text) if newline < 0 else newline + 1


def _blank_or_comment(text: str) -> bool:
    return all(not line.strip() or line.lstrip().startswith("#") for line in text.split("\n"))


# --- the 127 suggestion (rule 4) -------------------------------------------------------


def _unresolved_word(command: str, snapshot) -> str | None:
    """The first word of a simple command that bash did not find, when it is safe to show.

    None when the text is not one simple command, when the word may have resolved (an
    alias, a builtin or keyword, a path, an executable on ``$PATH``), when a ``$PATH``
    entry depends on the session's directory, or when the word is not safe to display.
    """
    if any(ch in _NOT_SIMPLE for ch in command):
        return None
    pieces = command.split()
    if not pieces or _ASSIGNMENT.match(pieces[0]):
        return None
    word = first_token(command)
    if not _displayable(word) or "/" in word:
        return None
    if word in _parse_aliases(snapshot.aliases):
        return None
    disabled = set(_DISABLED_LINE.findall(snapshot.disabled_builtins))
    if word in BASH_KEYWORDS or (word in BASH_BUILTINS and word not in disabled):
        return None
    for entry in snapshot.path.split(":"):
        if not os.path.isabs(entry):
            return None  # empty, `.` or relative: bash may have found it there
        candidate = os.path.join(entry, word)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return None
    return word


def _packages(lookup: Lookup | None, word: str) -> tuple[str, ...]:
    """The provider's answer, shown only when every name is safe to display."""
    if lookup is None:
        return ()
    try:
        answer = lookup(word)
        if answer is None:
            return ()
        if isinstance(answer, str):
            answer = (answer,)
        names = list(itertools.islice(answer, MAX_LOOKUP_ITEMS))
    except Exception:
        return ()
    shown: list[str] = []
    for name in names:
        if isinstance(name, str) and _displayable(name) and name not in shown:
            shown.append(name)
            if len(shown) == MAX_PACKAGES:
                break
    return tuple(shown)


def _displayable(text: str) -> bool:
    """Printable ASCII without spaces, 1 to MAX_SHOWN characters: no escape sequences,
    no bidi controls, no look-alikes."""
    return 0 < len(text) <= MAX_SHOWN and all("!" <= ch <= "~" for ch in text)


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        pass
    low, high = int(signal.SIGRTMIN), int(signal.SIGRTMAX)
    if low < number <= high:
        return f"SIGRTMIN+{number - low}"
    return f"signal {number}"
