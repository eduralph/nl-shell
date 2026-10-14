"""The read-eval loop.

M0.1 scaffold: reads lines and handles the exit commands. Routing, the policy gate and
the PTY session arrive in later M0 issues (see docs/design/architecture/05-building-block-view.md).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO
import sys

PROMPT = "nl> "
EXIT_COMMANDS = frozenset({":quit", ":exit"})
NOT_YET = "nl-shell can't run anything yet: the bash session arrives in M0.2."


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
            print(NOT_YET, file=self._out)
