"""The prompts YAML and the model input built from it.

Two layers, read with PyYAML's safe loader only (ADR-0020 §1-§2, ADR-0023):

1. the active target's default, ``<target folder>/prompts/prompts.yaml`` (required);
2. the user's ``$XDG_CONFIG_HOME/nl-shell/prompts.yaml`` (optional), which overrides the
   default key by key: a key it sets replaces the default's value whole.

The keys, all three required once the layers are merged:

    system            the system prompt: a non-empty string
    examples          the few-shot examples: a list of mappings, each with exactly
                      `request` (a non-empty string) and `response` (a string that is a
                      valid proposal/v1 answer)
    request_template  the volatile part: a string holding `{state}` once and ending in
                      `{request}`, its only other placeholder

Any other key, a key with the wrong type, YAML that does not parse, or a top level that is
not a mapping is a ``PromptsError`` naming the file; ``nl_shell.cli`` refuses to start on
it, as it does for a malformed ``target.toml``.

The model input (``build``) is an ordered message list, stable part first (ADR-0018 §4):
the system prompt, then each example as a user and an assistant message in file order,
then the request as the last user message. ``render_input`` is its text form, the one
``:context`` prints: each message as ``--- <role> ---`` and its content (escaped,
backslashes too), joined with LF, with no trailing LF.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from nl_shell import config, proposal, render

PROMPTS_NAME = "prompts.yaml"
PROMPTS_DIR = "prompts"
KEYS = ("system", "examples", "request_template")
EXAMPLE_KEYS = ("request", "response")
STATE = "{state}"
REQUEST = "{request}"
# The state block (#11) fills this slot; until then it is empty.
EMPTY_STATE = ""


class PromptsError(Exception):
    """A prompts file could not be used; the message names the file and the problem."""


class Message(NamedTuple):
    role: str  # "system", "user" or "assistant"
    content: str


@dataclass(frozen=True)
class Example:
    request: str
    response: str


@dataclass(frozen=True)
class Prompts:
    """The merged prompts, and the files they came from, in load order."""

    system: str
    examples: tuple[Example, ...]
    request_template: str
    files: tuple[Path, ...]

    def status_lines(self) -> tuple[str, ...]:
        """The ``:status`` lines: one per loaded file."""
        return tuple(f"prompts: {path}" for path in self.files)


def default_path(target_folder: Path) -> Path:
    return target_folder / PROMPTS_DIR / PROMPTS_NAME


def user_path(env: Mapping[str, str] | None = None) -> Path:
    """The user's prompts.yaml: next to config.toml (ADR-0020 §1)."""
    return config.config_path(env).parent / PROMPTS_NAME


def load(target_folder: Path, user_file: Path | None = None) -> Prompts:
    """Load the target's default prompts, overridden by ``user_file`` if it exists."""
    default = Path(os.path.abspath(default_path(target_folder)))
    merged = dict(_read(default, required=True))
    files = [default]
    if user_file is not None:
        user = Path(os.path.abspath(user_file))
        layer = _read(user, required=False)
        if layer is not None:
            merged.update(layer)
            files.append(user)
    missing = [key for key in KEYS if key not in merged]
    if missing:
        names = ", ".join(f"`{key}`" for key in missing)
        raise PromptsError(f"{files[-1]}: missing required key(s) {names}")
    return Prompts(
        system=merged["system"][0],
        examples=merged["examples"][0],
        request_template=merged["request_template"][0],
        files=tuple(files),
    )


def _read(path: Path, *, required: bool) -> dict[str, tuple[Any, Path]] | None:
    """One layer: each known key's validated value and the file it came from."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise PromptsError(f"{path}: missing") from None
        return None
    except OSError as exc:
        raise PromptsError(f"{path}: cannot read: {exc.strerror or exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PromptsError(f"{path}: not valid YAML: not valid UTF-8") from None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PromptsError(f"{path}: not valid YAML: {exc}") from None
    except (ValueError, RecursionError) as exc:
        # The safe loader also raises these: int() over Python's digit limit or a bad
        # timestamp gives ValueError, and deep nesting overflows its recursive composer.
        raise PromptsError(
            f"{path}: not valid YAML: {type(exc).__name__}: {exc}"
        ) from None
    if doc is None:
        doc = {}  # an empty file overrides nothing
    if not isinstance(doc, dict):
        raise PromptsError(f"{path}: the top level must be a mapping of keys")
    layer: dict[str, tuple[Any, Path]] = {}
    for key, value in doc.items():
        if key not in KEYS:
            known = ", ".join(KEYS)
            raise PromptsError(f"{path}: unknown key {key!r} (known: {known})")
        layer[key] = (_VALIDATORS[key](path, value), path)
    return layer


def _system(path: Path, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptsError(f"{path}: `system` must be a non-empty string")
    return value


def _examples(path: Path, value: Any) -> tuple[Example, ...]:
    if not isinstance(value, list):
        raise PromptsError(f"{path}: `examples` must be a list")
    examples = []
    for number, item in enumerate(value, start=1):
        where = f"{path}: `examples` item {number}"
        if not isinstance(item, dict) or sorted(item, key=str) != sorted(EXAMPLE_KEYS):
            raise PromptsError(
                f"{where} must be a mapping with exactly `request` and `response`"
            )
        request, response = item["request"], item["response"]
        if not isinstance(request, str) or not request.strip():
            raise PromptsError(f"{where}: `request` must be a non-empty string")
        if not isinstance(response, str):
            raise PromptsError(f"{where}: `response` must be a string")
        if isinstance(proposal.parse(response), proposal.Rejected):
            raise PromptsError(f"{where}: `response` is not a valid proposal/v1 answer")
        examples.append(Example(request=request, response=response))
    return tuple(examples)


def _request_template(path: Path, value: Any) -> str:
    # Ending in the only {request} also puts the one {state} before it.
    if (
        not isinstance(value, str)
        or value.count(STATE) != 1
        or value.count(REQUEST) != 1
        or not value.endswith(REQUEST)
    ):
        raise PromptsError(
            f"{path}: `request_template` must be a string holding {STATE} once "
            f"and ending in {REQUEST} (the request goes last, ADR-0018 §4)"
        )
    return value


_VALIDATORS = {
    "system": _system,
    "examples": _examples,
    "request_template": _request_template,
}


def volatile(template: str, request: str, state: str = EMPTY_STATE) -> str:
    """Fill the template. Plain splicing, so braces in the request stay as typed."""
    head, _, rest = template.partition(STATE)
    middle = rest[: -len(REQUEST)]
    return head + state + middle + request


def build(prompts: Prompts, request: str) -> tuple[Message, ...]:
    """The model input for one request, stable part first (ADR-0018 §4)."""
    messages = [Message("system", prompts.system)]
    for example in prompts.examples:
        example_input = volatile(prompts.request_template, example.request)
        messages.append(Message("user", example_input))
        messages.append(Message("assistant", example.response))
    messages.append(Message("user", volatile(prompts.request_template, request)))
    return tuple(messages)


def render_input(messages: Sequence[Message]) -> str:
    """The text form ``:context`` prints: exactly the message list, escaped."""
    return "\n".join(
        f"--- {role} ---\n" + render.escape(content, backslash=True)
        for role, content in messages
    )
