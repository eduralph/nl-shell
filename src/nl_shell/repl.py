"""The read-eval loop.

Reads lines, handles the exit commands, then the input-mode prefixes
(``nl_shell.input_modes``), and routes everything else as a shell command or natural
language (``nl_shell.router``). Natural language and ``@request`` go through the prompts
(``nl_shell.prompts``), the model backend (``nl_shell.backend``) and the strict
``proposal/v1`` parser (``nl_shell.proposal``); the proposal is shown, never run. Nothing
runs yet: the policy gate and the PTY session arrive in later M0 issues (see
docs/design/architecture/05-building-block-view.md).

Every reply is printed through ``nl_shell.render.escape``, so no text from the model, a
file or the user reaches the screen with a hidden character in it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TextIO
import sys

from nl_shell import input_modes, render
from nl_shell.input_modes import Mode, StatusLine
from nl_shell.router import Route, route

if TYPE_CHECKING:
    from nl_shell.backend import Backend
    from nl_shell.prompts import Message, Prompts

PROMPT = "nl> "
COMMAND_NOT_YET = (
    "That looks like a shell command, but nl-shell can't run anything yet: "
    "the bash session arrives in M0.2."
)
NO_BACKEND = (
    "No model backend is configured, so nl-shell can't answer this yet: "
    "the model client arrives with #77."
)
NO_USABLE_PROPOSAL = "No usable proposal came back from the model."
NOTHING_RUNS_YET = (
    "Nothing runs yet: running a proposal arrives with the policy gate (#12, #78)."
)
REASON_TEXT = {
    "unclear-request": "the request is unclear",
    "not-a-shell-task": "this is not a shell task",
    "unsafe-request": "the request looks unsafe",
    "missing-information": "some information is missing",
}
CLARIFY = "The model needs more from you before it can propose a command: {reason}."
REFUSE = "The model will not propose a command for this: {reason}."


def show_proposal(result: object) -> str:
    """The reply for one parsed model answer (``nl_shell.proposal.parse``)."""
    from nl_shell import proposal

    if isinstance(result, proposal.Command):
        lines = result.command.split("\n")
        count = f"{len(lines)} line" + ("" if len(lines) == 1 else "s")
        framed = [f"Proposed command (risk: {result.risk}, {count}):"]
        framed += [f"  | {line}" for line in lines]
        framed.append(NOTHING_RUNS_YET)
        return "\n".join(framed)
    if isinstance(result, proposal.Answer):
        template = CLARIFY if result.kind == "clarify" else REFUSE
        return template.format(reason=REASON_TEXT[result.reason])
    return NO_USABLE_PROPOSAL


class Repl:
    """One REPL session. ``read`` returns the next line or raises EOFError."""

    def __init__(
        self,
        read: Callable[[str], str],
        out: TextIO,
        status_lines: Sequence[StatusLine] = (),
        *,
        backend: Backend | None = None,
        prompts: Prompts | None = None,
    ) -> None:
        if backend is not None and prompts is None:
            raise ValueError("a Repl with a backend needs the prompts to build its input")
        self._read = read
        self._out = out
        # This session's own `:status` lines, after the version line
        # (see the `StatusLine` comment in nl_shell.input_modes).
        self._status_lines = tuple(status_lines)
        # No backend is the interactive default until #77 adds the model client.
        self._backend = backend
        self._prompts = prompts
        # The model input of the last request, for `:context`.
        self._last_input: tuple[Message, ...] | None = None

    @classmethod
    def interactive(
        cls,
        status_lines: Sequence[StatusLine] = (),
        *,
        backend: Backend | None = None,
        prompts: Prompts | None = None,
    ) -> "Repl":
        from prompt_toolkit import PromptSession

        session: PromptSession[str] = PromptSession()
        return cls(
            session.prompt, sys.stdout, status_lines, backend=backend, prompts=prompts
        )

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
            if input_modes.is_exit(text):
                return 0
            print(render.escape(self._reply(text)), file=self._out)

    def _reply(self, text: str) -> str:
        """The reply for one stripped, non-blank, non-exit line."""
        prefixed = input_modes.parse(text)
        if prefixed is None:
            if route(text) is Route.COMMAND:
                return COMMAND_NOT_YET
            return self._propose(text)
        if prefixed.mode is Mode.COMMAND and prefixed.payload:
            # `\cmd` skips only the intent classifier (here, the router); it never skips
            # the policy gate. ADR-0013 §7 (Proposed; #64 closed on it). The gate is wired
            # in by #78, so `prefixed.payload` is the command text the gate will receive.
            return COMMAND_NOT_YET
        if prefixed.mode is Mode.PROPOSE and prefixed.payload:
            return self._propose(prefixed.payload)
        return input_modes.reply(prefixed, self._status_lines, self._model_input_text())

    def _propose(self, request: str) -> str:
        """Prompt, backend and strict parser for one request. Shows; runs nothing."""
        if self._backend is None or self._prompts is None:
            return NO_BACKEND
        from nl_shell import prompts, proposal

        messages = prompts.build(self._prompts, request)
        self._last_input = messages
        raw = self._backend.complete(messages)
        return show_proposal(proposal.parse(raw))

    def _model_input_text(self) -> str | None:
        """The ``:context`` text of the last model input, or None before any request."""
        if self._last_input is None:
            return None
        from nl_shell import prompts

        return prompts.render_input(self._last_input)
