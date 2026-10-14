"""The read-eval loop.

Reads lines, handles the exit commands, and routes everything else as a shell command or
natural language (``nl_shell.router``). Nothing runs yet: the policy gate, the PTY session
and the model backend arrive in later M0 issues (see
docs/design/architecture/05-building-block-view.md).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO
import sys

from nl_shell.router import Route, route

PROMPT = "nl> "
EXIT_COMMANDS = frozenset({":quit", ":exit"})
COMMAND_NOT_YET = (
    "That looks like a shell command, but nl-shell can't run anything yet: "
    "the bash session arrives in M0.2."
)
NATURAL_LANGUAGE_NOT_YET = (
    "That looks like a natural-language request, but nl-shell can't answer it yet: "
    "the model backend arrives in M0.8."
)
REPLIES = {
    Route.COMMAND: COMMAND_NOT_YET,
    Route.NATURAL_LANGUAGE: NATURAL_LANGUAGE_NOT_YET,
}


class Repl:
    """One REPL session. ``read`` returns the next line or raises EOFError."""

    def __init__(self, read: Callable[[str], str], out: TextIO) -> None:
        self._read = read
        self._out = out

    @classmethod
    def interactive(cls) -> "Repl":
        from prompt_toolkit import PromptSession

        session: PromptSession[str] = PromptSession()
        return cls(session.prompt, sys.stdout)

    def run(self) -> int:
        """Loop until an exit command or end of input. Returns the exit status."""
        while True:
            try:
                line = self._read(PROMPT)
            except EOFError:
                return 0
            except KeyboardInterrupt:
                # Ctrl-C clears the current line, as in bash; Ctrl-D exits.
                continue
            text = line.strip()
            if not text:
                continue
            if text in EXIT_COMMANDS:
                return 0
            print(REPLIES[route(text)], file=self._out)
