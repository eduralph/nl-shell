"""The M0 input router: a heuristic that tells shell commands from natural language.

M0.7 placeholder. If the first token of the input is not a bash builtin or an
executable (found on ``$PATH``, or named by a path containing ``/``), the input is
natural language. If it is, the input is a command, unless it reads like English:

- An unbalanced single quote is English on its own: the line holds an odd number of
  ``'``, no ``"`` and no ``\\`` ("find what's using port 80", "echo it's fine").
- Otherwise the line is English when all of these hold: the first token is not
  ``echo`` or ``printf`` (their operands are free text); the line has at least 4
  words; no word starts with ``-`` and the line holds no shell metacharacter (any of
  ``|&;<>$*=(){}[]~/``, a backtick or a double quote); and at least one word, in
  lower case, is a common English function word (``the``, ``in``, ``my``, ``is``…).

A "word" here is an item of ``text.split()``, a plain whitespace split, so the rule is
defined even on lines shlex cannot lex. A misroute is a usability cost, not a safety
one: a command still has to pass the gate, and ``\\cmd`` always forces bash.

The M3 intent classifier replaces this and keeps it as its fallback, so it stays a unit
of its own with no REPL dependency.

Known limitations: short English ("ls my files"), English with no function word, and
English holding a metacharacter ("find big files in ~/src") still route as a command.
Bash keywords (``if``, ``for``…), leading variable assignments, aliases and shell
functions are not recognised.
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


# Commands whose operands are free text, so English after them is still a command.
FREE_TEXT_COMMANDS = frozenset({"echo", "printf"})

# Characters that make a line shell rather than English.
SHELL_METACHARACTERS = frozenset('|&;<>$*=(){}[]~/`"')

# Common English function words: one of them in a long, plain line marks a sentence.
FUNCTION_WORDS = frozenset(
    """
    a an the my me all any some this that these those which what who how in on of for
    from to with by is are was were than
    """.split()
)


def _has_unbalanced_single_quote(text: str) -> bool:
    """An odd number of ``'``, no ``"`` and no ``\\``: an apostrophe, not quoting."""
    return text.count("'") % 2 == 1 and '"' not in text and "\\" not in text


def _reads_like_english(token: str, text: str) -> bool:
    """Whether a line whose first token is a command is really an English sentence."""
    if _has_unbalanced_single_quote(text):
        return True
    if token in FREE_TEXT_COMMANDS:
        return False
    words = text.split()
    if len(words) < 4:
        return False
    if any(word.startswith("-") for word in words):
        return False
    if any(char in SHELL_METACHARACTERS for char in text):
        return False
    return any(word.lower() in FUNCTION_WORDS for word in words)


def _is_command_name(token: str) -> bool:
    if token in BASH_BUILTINS:
        return True
    if "/" in token:
        return is_executable_file(token)
    return shutil.which(token) is not None


def route(text: str) -> Route:
    """Route one input line. ``$PATH`` is read on every call, never cached."""
    token = first_token(text)
    if not token or not _is_command_name(token):
        return Route.NATURAL_LANGUAGE
    if _reads_like_english(token, text):
        return Route.NATURAL_LANGUAGE
    return Route.COMMAND
