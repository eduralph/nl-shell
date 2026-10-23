"""Model backends: what takes the model input and returns the model's raw answer.

A backend gets the model input (the ordered message list ``nl_shell.prompts.build``
makes) and returns the response text, unparsed; the REPL parses it
(``nl_shell.proposal``). ``nl_shell.teacher_client`` is the HTTP client to the local model
server; ``RecordingStub`` is the backend for tests (#13, #78): it returns the responses the
test gives it, in order, and records every model input it was sent.

A request may name the schema its answer is constrained to (``ResponseSchema``); ``None``,
the default, means the ``proposal/v1`` schema, as for every command-model request. The
quarantined reader (``nl_shell.reader``) names its own (ADR-0017 §5).

A backend that cannot get an answer raises ``BackendError``; the REPL shows one fixed reply
for it and keeps running. Its text is shown to the user, so a backend escapes any text
that came from the server (``nl_shell.render.escape``) when it builds the error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from nl_shell.prompts import Message


@dataclass(frozen=True)
class ResponseSchema:
    """A JSON schema a request's answer is constrained to, and the name it is sent under."""

    name: str
    schema: Mapping[str, Any]


class Backend(Protocol):
    def complete(
        self, messages: Sequence[Message], *, schema: ResponseSchema | None = None
    ) -> str:
        """The raw response text for one model input; ``schema=None``: proposal/v1."""
        ...


class BackendError(Exception):
    """The backend could not get a model answer. The text says why."""


class StubExhausted(Exception):
    """The stub was sent more requests than it was given responses."""


class RecordingStub:
    """A backend that returns fixed responses and records every input, in order."""

    def __init__(self, responses: Iterable[str] = ()) -> None:
        self._responses = list(responses)
        # One entry per request, in order: the message tuple the backend was handed.
        self.inputs: list[tuple[Message, ...]] = []
        # One entry per request, in the same order: the schema it named (None: proposal/v1).
        self.schemas: list[ResponseSchema | None] = []

    def respond(self, *responses: str) -> None:
        """Queue more responses."""
        self._responses.extend(responses)

    def complete(
        self, messages: Sequence[Message], *, schema: ResponseSchema | None = None
    ) -> str:
        self.inputs.append(tuple(messages))
        self.schemas.append(schema)
        if not self._responses:
            raise StubExhausted(f"no response queued for request {len(self.inputs)}")
        return self._responses.pop(0)
