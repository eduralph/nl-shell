"""The read-only grammar (ADR-0027, spec ``policy-gate/v2`` §2).

The only commands the gate can ever class ``read-only`` are the ones this module
recognises. The grammar is closed and small enough that bash performs no expansion on a
string it accepts, and quote removal is the only thing bash does to its words, so the
words ``recognise`` produces are the words bash runs. Everything else is *outside the
grammar*: the gate does not analyse it further for read-only and the user confirms it as
written.

The grammar::

    list      := pipeline ( ( "||" | "&&" | ";" ) pipeline )* [ ";" ]
    pipeline  := simple ( "|" simple )*
    simple    := ( word | redirect )+          -- at least one word
    redirect  := "2>&1" | [ "2" | "&" ] ">" /dev/null | "<" word
    word      := piece+                        -- adjacent pieces form one word
    piece     := bare | "'" [^'\\n]* "'" | '"' [^"$`\\\\!\\n]* '"'
    bare      := [A-Za-z0-9_./:@%+,=-]+

Separators are spaces and tabs only. The first word of a simple command (the command
word) MUST NOT contain ``=`` and MUST NOT be a single bare piece spelling a bash reserved
word. Nothing else is accepted: no ``~``, glob, brace, ``$``, backtick, ``#``, ``!``,
``&``, newline, subshell, group, compound command, function or prefix assignment.

``recognise`` is linear in the input and never raises on a string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nl_shell.gate.types import PIPE, TERMINAL, Invocation, Redirect, Stdin, Word

MAX_INPUT_BYTES = 8 * 1024

BARE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:@%+,=-")
RESERVED_WORDS = frozenset(
    (
        "if", "then", "else", "elif", "fi", "do", "done", "case", "esac", "while",
        "until", "for", "in", "select", "function", "time", "coproc", "{", "}", "!",
        "[[", "]]",
    )
)  # fmt: skip

_SQ = re.compile(r"'[^'\n]*'")
_DQ = re.compile(r'"[^"$`\\!\n]*"')
# A redirect spelling ends where a token ends: at the end, a separator or an operator.
# Without the boundary `>/dev/null.bak` would read as `>/dev/null` plus a word `.bak`,
# while bash writes to a real file.
_TOKEN_END = r"(?=$|[ \t|;&<>])"
_DEV_NULL = re.compile(r"(2>|&>|>)[ \t]*/dev/null" + _TOKEN_END)
_STDERR_DUP = re.compile(r"2>&1" + _TOKEN_END)
_SOCKETS = ("/dev/tcp/", "/dev/udp/")


@dataclass(frozen=True)
class Recognised:
    """A command the grammar accepts: its simple commands, in source order."""

    invocations: tuple[Invocation, ...]


@dataclass
class _Simple:
    words: list[Word]
    redirects: list[Redirect]


def recognise(source: str) -> Recognised | None:
    """The ``Recognised`` form of ``source``, or ``None`` when it is outside the grammar."""
    if not isinstance(source, str) or not source:
        return None
    if len(source.encode("utf-8", "surrogatepass")) > MAX_INPUT_BYTES:
        return None
    pipelines = _parse(source)
    if pipelines is None:
        return None
    invocations: list[Invocation] = []
    for pipeline in pipelines:
        for position, simple in enumerate(pipeline):
            stdin: Stdin = PIPE if position else TERMINAL
            for redirect in simple.redirects:
                if redirect.operator == "<" and redirect.target is not None:
                    stdin = Stdin("file", redirect.target.text)
            invocations.append(
                Invocation(
                    kind="command",
                    words=tuple(simple.words),
                    assignments=(),
                    redirects=tuple(simple.redirects),
                    stdin=stdin,
                )
            )
    return Recognised(tuple(invocations))


def argv(source: str) -> list[list[str]] | None:
    """The word texts of each simple command, in source order (for the bash oracle
    test), or ``None`` outside the grammar."""
    found = recognise(source)
    if found is None:
        return None
    return [[w.text for w in i.words] for i in found.invocations]


# --- the recogniser ------------------------------------------------------------------


def _parse(source: str) -> list[list[_Simple]] | None:
    n = len(source)
    i = 0
    pipelines: list[list[_Simple]] = []
    current: list[_Simple] = []
    simple: _Simple | None = None
    pending: str | None = None  # the operator a command must follow

    while True:
        while i < n and source[i] in " \t":
            i += 1
        if i >= n:
            break
        c = source[i]

        if c in "|&;" and not source.startswith("&>", i):
            if simple is None:
                return None  # a leading operator, or two operators in a row
            if source.startswith("||", i) or source.startswith("&&", i):
                op, i = source[i : i + 2], i + 2
            elif c in "|;":
                op, i = c, i + 1
            else:
                return None  # `&` alone, `|&`, `;;` and the like
            current.append(simple)
            if op != "|":
                pipelines.append(current)
                current = []
            simple, pending = None, op
            continue

        if simple is None:
            simple, pending = _Simple([], []), None

        redirect = _redirect(source, i)
        if redirect is not None:
            record, i = redirect
            if record is None:
                return None
            simple.redirects.append(record)
            continue

        word = _word(source, i)
        if word is None:
            return None
        text, i = word
        if not simple.words:
            if "=" in text.source or not text.text:
                return None
            if text.source in RESERVED_WORDS:
                return None  # a single bare piece spelling a reserved word
        simple.words.append(text)

    if simple is None and pending not in (None, ";"):
        return None  # a trailing `|`, `&&` or `||`
    if simple is not None:
        current.append(simple)
    if current:
        pipelines.append(current)
    if not pipelines or any(not s.words for p in pipelines for s in p):
        return None
    return pipelines


def _redirect(source: str, i: int) -> tuple[Redirect | None, int] | None:
    """A redirect starting at ``i``: ``(record, next)``, ``(None, next)`` when a redirect
    operator is there but not in an accepted form, or ``None`` when no redirect starts
    here. Only tried at the start of a word."""
    c = source[i]
    if _STDERR_DUP.match(source, i):
        return _record("2", ">&", "1", source[i : i + 4], output=True, exempt=True), i + 4
    match = _DEV_NULL.match(source, i)
    if match is not None:
        op = match.group(1)
        descriptor = {"2>": "2", "&>": None, ">": None}[op]
        operator = "&>" if op == "&>" else ">"
        record = _record(descriptor, operator, "/dev/null", match.group(0), output=True, exempt=True)
        return record, match.end()
    if c == "<":
        j = i + 1
        while j < len(source) and source[j] in " \t":
            j += 1
        if source.startswith("<", j) or source.startswith("<", i + 1):
            return None, j  # `<<`, `<<<`, `<(`
        word = _word(source, j)
        if word is None:
            return None, j
        target, end = word
        return _record(None, "<", target.text, source[i:end], output=False, exempt=False, target=target), end
    if c in ">&" or (c in "0123456789" and _digits_then_redirect(source, i)):
        return None, i
    return None


def _digits_then_redirect(source: str, i: int) -> bool:
    j = i
    while j < len(source) and source[j].isdigit():
        j += 1
    return j < len(source) and source[j] in "<>"


def _record(
    descriptor: str | None,
    operator: str,
    target_text: str,
    display: str,
    *,
    output: bool,
    exempt: bool,
    target: Word | None = None,
) -> Redirect:
    if target is None:
        target = Word(text=target_text, literal=True, source=target_text)
    return Redirect(
        kind="file_redirect",
        descriptor=descriptor,
        operator=operator,
        target=target,
        output=output,
        exempt=exempt,
        device_socket=any(s in target.text for s in _SOCKETS),
        feed=None,
        display=display,
    )


def _word(source: str, i: int) -> tuple[Word, int] | None:
    """A word starting at ``i``: adjacent bare, single-quoted and double-quoted pieces."""
    n = len(source)
    start = i
    text: list[str] = []
    while i < n:
        c = source[i]
        if c in BARE_CHARS:
            j = i
            while j < n and source[j] in BARE_CHARS:
                j += 1
            # A digit run right before `<` or `>` is a descriptor for bash, not a word.
            if j < n and source[j] in "<>" and source[i:j].isdigit():
                return None
            text.append(source[i:j])
            i = j
        elif c == "'":
            match = _SQ.match(source, i)
            if match is None:
                return None
            text.append(match.group(0)[1:-1])
            i = match.end()
        elif c == '"':
            match = _DQ.match(source, i)
            if match is None:
                return None
            text.append(match.group(0)[1:-1])
            i = match.end()
        else:
            break
    if i == start:
        return None
    return Word(text="".join(text), literal=True, source=source[start:i]), i
