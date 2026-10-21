"""Input-mode prefixes: the explicit overrides checked before the heuristic router.

``docs/plan.md`` §Input Modes defines them. A prefix always wins over the router, even
when the line is also valid bash (``?x`` is a glob, ``\\ls`` is valid bash):

    prefix   mode                     today
    \\cmd     shell command            the command route (the router is skipped)
    ?q       ask mode                 placeholder reply (M0.8)
    @req     one-command proposal     the model's proposal, shown (nl_shell.repl)
    @@goal   agent loop               reserved reply; #76 owns the grammar (M0.6b)
    :name    meta-command             :status and :context work; :history (#17)

This stays a unit of its own, apart from ``nl_shell.router``: the router is the
plain-input heuristic the M3 classifier (#40) replaces, and prefixes are not heuristics.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
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


# One `:status` line: a zero-argument callable returning the line's text.
StatusLine = Callable[[], str]

# `:status` prints the version line, then the session's own lines in order. The session's
# lines are not module state: whoever builds the session passes them to
# `Repl(..., status_lines=...)` (or `Repl.interactive(status_lines=...)`), the Repl hands
# them to `reply`, and `reply` to `status`. So a line never leaks into another Repl or a
# later run in the same process, and a Repl built with no lines shows only the version.
# A later issue adds a line by adding its callable to the tuple `nl_shell.cli.main` builds
# for the session, after the active-target and loaded-prompt-file lines (#77 endpoint and
# backend); nothing here changes.
def version_line() -> str:
    return f"nl-shell {nl_shell.__version__}"


def status(session_lines: Sequence[StatusLine] = ()) -> str:
    return "\n".join(line() for line in (version_line, *session_lines))


ASK_NOT_YET = (
    "That is an ask-mode question (?), but nl-shell can't answer it yet: "
    "ask mode arrives in M0.8."
)
AGENT_RESERVED = "@@ is reserved for the agent loop, which arrives with M0.6b (#76)."
NO_MODEL_INPUT_YET = (
    "No model input yet: :context shows the model input of the last request."
)
HISTORY_NOT_YET = ":history is not available yet: it arrives with M0.9 (#17)."


def context(model_input: str | None) -> str:
    """``:context``: the last request's model input, in the text form
    ``nl_shell.prompts.render_input`` makes (already escaped), or a fixed reply."""
    return NO_MODEL_INPUT_YET if model_input is None else model_input


# Meta-commands by exact, case-sensitive name. Each takes the session's `:status` lines
# (only `:status` uses them) and the `:context` text of the last model input, or None
# (only `:context` uses it). The exit commands (EXIT_NAMES) are handled by the REPL loop
# before dispatch (`is_exit`), so they are listed as known but not looked up here.
META_COMMANDS: dict[str, Callable[[Sequence[StatusLine], str | None], str]] = {
    "context": lambda _status_lines, model_input: context(model_input),
    "history": lambda _status_lines, _model_input: HISTORY_NOT_YET,
    "status": lambda status_lines, _model_input: status(status_lines),
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
    Mode.AGENT: AGENT_RESERVED,
}


def reply(
    prefixed: Prefixed,
    status_lines: Sequence[StatusLine] = (),
    model_input: str | None = None,
) -> str:
    """The reply for a prefixed line in any mode but a non-empty ``\\`` or ``@`` line.

    A ``\\`` command or an ``@`` request with a payload belongs to the REPL, which owns the
    command route and the model backend. ``status_lines`` are the session's own
    ``:status`` lines, after the version line; ``model_input`` is the ``:context`` text of
    the session's last model input, or None before any request.
    """
    mode, payload = prefixed
    if mode is Mode.META:
        if not payload:
            return f"Usage: :<name> runs a meta-command. Known: {known_meta_commands()}."
        command = META_COMMANDS.get(payload)
        if command is None:
            return f"Unknown meta-command :{payload}. Known: {known_meta_commands()}."
        return command(status_lines, model_input)
    if not payload:
        return USAGE[mode]
    return REPLIES[mode]
