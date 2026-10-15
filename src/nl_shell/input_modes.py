"""Input-mode prefixes: the explicit overrides checked before the heuristic router.

``docs/plan.md`` §Input Modes defines them. A prefix always wins over the router, even
when the line is also valid bash (``?x`` is a glob, ``\\ls`` is valid bash):

    prefix   mode                     today
    \\cmd     shell command            the command route (the router is skipped)
    ?q       ask mode                 placeholder reply (M0.8)
    @req     one-command proposal     placeholder reply (M0.8)
    @@goal   agent loop               reserved reply; #76 owns the grammar (M0.6b)
    :name    meta-command             :status works; :context (#16), :history (#17)

This stays a unit of its own, apart from ``nl_shell.router``: the router is the
plain-input heuristic the M3 classifier (#40) replaces, and prefixes are not heuristics.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import NamedTuple

import nl_shell

# The meta-commands that end the REPL loop. Matched on the parsed `:` payload, like every
# other meta-command, so `: quit` exits just as `:quit` does.
EXIT_NAMES = ("quit", "exit")


class Mode(enum.Enum):
    COMMAND = "command"
    ASK = "ask"
    PROPOSE = "propose"
    AGENT = "agent"
    META = "meta"


class Prefixed(NamedTuple):
    """One prefixed line: its mode and the text after the prefix."""

    mode: Mode
    payload: str


# Longest prefix first: `@@` must be checked before `@`.
PREFIXES: tuple[tuple[str, Mode], ...] = (
    ("@@", Mode.AGENT),
    ("@", Mode.PROPOSE),
    ("?", Mode.ASK),
    ("\\", Mode.COMMAND),
    (":", Mode.META),
)


def parse(text: str) -> Prefixed | None:
    """The prefix mode of a stripped line, or None when it has no prefix.

    The payload is the rest of the line, stripped, for every prefix: ``: status`` names
    the meta-command ``status``.
    """
    for prefix, mode in PREFIXES:
        if text.startswith(prefix):
            return Prefixed(mode, text[len(prefix):].strip())
    return None


def is_exit(text: str) -> bool:
    """True when a stripped line is an exit command (``:quit``, ``: exit``, ...)."""
    prefixed = parse(text)
    return (
        prefixed is not None
        and prefixed.mode is Mode.META
        and prefixed.payload in EXIT_NAMES
    )


# `:status` prints one line per entry, in list order. Each entry is a zero-argument
# callable returning one line. Later issues add their lines by appending here (#92 active
# target, #16 prompt files loaded, #77 endpoint and backend), with no other change.
STATUS_LINES: list[Callable[[], str]] = [
    lambda: f"nl-shell {nl_shell.__version__}",
]


def status() -> str:
    return "\n".join(line() for line in STATUS_LINES)


ASK_NOT_YET = (
    "That is an ask-mode question (?), but nl-shell can't answer it yet: "
    "ask mode arrives in M0.8."
)
PROPOSE_NOT_YET = (
    "That is a one-command request (@), but nl-shell can't propose a command yet: "
    "command proposals arrive in M0.8."
)
AGENT_RESERVED = "@@ is reserved for the agent loop, which arrives with M0.6b (#76)."
CONTEXT_NOT_YET = ":context is not available yet: it arrives with M0.8 (#16)."
HISTORY_NOT_YET = ":history is not available yet: it arrives with M0.9 (#17)."

# Meta-commands by exact, case-sensitive name. The exit commands (EXIT_NAMES) are handled
# by the REPL loop before dispatch (`is_exit`), so they are listed as known but not looked
# up here.
META_COMMANDS: dict[str, Callable[[], str]] = {
    "context": lambda: CONTEXT_NOT_YET,
    "history": lambda: HISTORY_NOT_YET,
    "status": status,
}


def known_meta_commands() -> str:
    return ", ".join(f":{name}" for name in (*META_COMMANDS, *EXIT_NAMES))


USAGE = {
    Mode.COMMAND: "Usage: \\<command> treats <command> as a shell command, "
    "whatever the router would say.",
    Mode.ASK: "Usage: ?<question> asks a question in ask mode.",
    Mode.PROPOSE: "Usage: @<request> asks for one proposed shell command.",
    Mode.AGENT: "Usage: @@<goal> hands a goal to the agent loop "
    "(arrives with M0.6b, #76).",
}

REPLIES = {
    Mode.ASK: ASK_NOT_YET,
    Mode.PROPOSE: PROPOSE_NOT_YET,
    Mode.AGENT: AGENT_RESERVED,
}


def reply(prefixed: Prefixed) -> str:
    """The reply for a prefixed line in any mode but a non-empty ``\\`` command.

    A ``\\`` command with a payload belongs to the command route, which the REPL owns.
    """
    mode, payload = prefixed
    if mode is Mode.META:
        if not payload:
            return f"Usage: :<name> runs a meta-command. Known: {known_meta_commands()}."
        command = META_COMMANDS.get(payload)
        if command is None:
            return f"Unknown meta-command :{payload}. Known: {known_meta_commands()}."
        return command()
    if not payload:
        return USAGE[mode]
    return REPLIES[mode]
