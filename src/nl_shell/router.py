"""The M0 input router: a heuristic that tells shell commands from natural language.

M0.7 placeholder. If the first token of the input is a bash builtin or an executable
(found on ``$PATH``, or named by a path containing ``/``), the input is a command;
otherwise it is natural language. The M3 intent classifier replaces this and keeps it
as its fallback, so it stays a unit of its own with no REPL dependency.

Known limitation, on purpose: English that starts with a command name ("find large
files in my home") routes as a command. Bash keywords (``if``, ``for``…), leading
variable assignments, aliases and shell functions are not recognised.
"""

from __future__ import annotations

import enum
import os
import shlex
import shutil


class Route(enum.Enum):
    COMMAND = "command"
    NATURAL_LANGUAGE = "natural_language"


# Output of `compgen -b` in bash 5. Keywords (`if`, `for`, `while`…) are left out.
BASH_BUILTINS = frozenset(
    """
    . : [ alias bg bind break builtin caller cd command compgen complete compopt
    continue declare dirs disown echo enable eval exec exit export false fc fg getopts
    hash help history jobs kill let local logout mapfile popd printf pushd pwd read
    readarray readonly return set shift shopt source suspend test times trap true type
    typeset ulimit umask unalias unset wait
    """.split()
)


def first_token(text: str) -> str:
    """The first shell word of ``text``, or "" if there is none. Never raises."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    try:
        token = lexer.get_token()
    except ValueError:
        # Not valid shell quoting (e.g. "what's using port 80"): fall back to whitespace.
        words = text.split()
        return words[0] if words else ""
    return token or ""


def is_executable_file(path: str) -> bool:
    path = os.path.expanduser(path)
    return os.path.isfile(path) and os.access(path, os.X_OK)


def route(text: str) -> Route:
    """Route one input line. ``$PATH`` is read on every call, never cached."""
    token = first_token(text)
    if not token:
        return Route.NATURAL_LANGUAGE
    if token in BASH_BUILTINS:
        return Route.COMMAND
    if "/" in token:
        return Route.COMMAND if is_executable_file(token) else Route.NATURAL_LANGUAGE
    if shutil.which(token) is not None:
        return Route.COMMAND
    return Route.NATURAL_LANGUAGE
