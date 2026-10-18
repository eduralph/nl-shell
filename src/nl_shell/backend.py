"""Model backends: what takes the model input and returns the model's raw answer.

A backend gets the model input (the ordered message list ``nl_shell.prompts.build``
makes) and returns the response text, unparsed; the REPL parses it
(``nl_shell.proposal``). #77 adds the HTTP client to the local model server. Until then
the only backend is ``RecordingStub``, for tests (#13, #78): it returns the responses the
test gives it, in order, and records every model input it was sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from nl_shell.prompts import Message


class Backend(Protocol):
    def complete(self, messages: Sequence[Message]) -> str:
        """The raw response text for one model input."""
        ...


class StubExhausted(Exception):
    """The stub was sent more requests than it was given responses."""


class RecordingStub:
    """A backend that returns fixed responses and records every input, in order."""

    def __init__(self, responses: Iterable[str] = ()) -> None:
        self._responses = list(responses)
        # One entry per request, in order: the message tuple the backend was handed.
        self.inputs: list[tuple[Message, ...]] = []

    def respond(self, *responses: str) -> None:
        """Queue more responses."""
        self._responses.extend(responses)

    def complete(self, messages: Sequence[Message]) -> str:
        self.inputs.append(tuple(messages))
        if not self._responses:
            raise StubExhausted(f"no response queued for request {len(self.inputs)}")
        return self._responses.pop(0)
