"""The policy gate (ADR-0013, ADR-0027; spec ``docs/design/specs/policy-gate/v2.md``).

``classify(command, origin=..., label=...)`` runs the spec's §1 decision procedure and
returns a ``Result`` with the session-log ``gate`` shape
(``{"class", "reasons", "rejected"}``, spec ``session-log/v1``). It never raises.

The procedure, and which module carries which step:

1. **Characters** (§1.1): a hidden character rejects a proposal and makes typed input at
   least ``mutating``.
2. **Text scan** (``scan``, §4–§5): the outright rejections (descriptors 3 and above,
   the sudo rules) and the ``privileged`` class, decided over the raw text so they hold
   whatever the parsers make of it.
3. **The read-only grammar** (``grammar``, §2): the only path to ``read-only``. A command
   it accepts goes, per simple command, through the command layer (``rules``: the
   allowlist and the per-command rules), live-session resolution (``live``), the
   sensitive-path check (``sensitive``) and the word check (``words``). A command it
   does not accept is at least ``mutating``, "outside the read-only grammar", and is
   not analysed further for read-only.
4. **Advice** (``parse`` + ``advice``, §6): the tree-sitter walk, which may raise the
   class and word the confirm, and may fail without consequence.
5. **The model's label** (§1.7) raises, never lowers.

``rules``, ``live``, ``sensitive`` and ``words`` are still the fail-closed stubs of #196
(#197, #198, #192, #190, #191 replace them), so nothing is ``read-only`` yet.

Each reason that quotes input escapes it: no reason holds a C0 or C1 control character
or any character ``nl_shell.render.is_hidden`` reports.
"""

from __future__ import annotations

from dataclasses import dataclass

from nl_shell import render
from nl_shell.gate import advice, grammar, live, parse, rules, scan, sensitive, words
from nl_shell.gate.rules import Verdict
from nl_shell.gate.types import CLASSES, RANK, Invocation, Stdin, Word

__all__ = [
    "CLASSES",
    "ORIGINS",
    "OUTSIDE_GRAMMAR",
    "Invocation",
    "Result",
    "Stdin",
    "Verdict",
    "Word",
    "classify",
]

ORIGINS = ("proposal", "typed")
OUTSIDE_GRAMMAR = "outside the read-only grammar"
DEVICE_SOCKET = "uses /dev/tcp or /dev/udp"


@dataclass(frozen=True)
class Result:
    """The gate's answer: the session-log ``gate`` object."""

    cls: str
    reasons: tuple[str, ...]
    rejected: bool

    def as_log(self) -> dict:
        return {"class": self.cls, "reasons": list(self.reasons), "rejected": self.rejected}


def escape(text: str) -> str:
    """``text`` safe to put in a reason: backslashes doubled, and every C0/C1 control and
    every hidden character written as an escape."""
    out = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif code < 0x20 or 0x7F <= code <= 0x9F or render.is_hidden(char):
            out.append(render.escape_char(char))
        else:
            out.append(char)
    return "".join(out)


class _Tally:
    def __init__(self) -> None:
        self.rank = -1
        self.cls = "read-only"
        self.reasons: list[str] = []
        self.rejected = False

    def raise_to(self, cls: str, reason: str | None = None) -> None:
        rank = RANK[cls]
        if rank > self.rank or (rank == self.rank and cls == "session-altering"):
            self.rank, self.cls = rank, cls
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    def reject(self, reason: str) -> None:
        self.rejected = True
        self.raise_to("mutating", reason)

    def verdict(self, verdict: Verdict) -> None:
        if verdict.cls not in RANK:
            raise ValueError("unknown class from a later step")
        for reason in verdict.reasons or (None,):
            self.raise_to(verdict.cls, reason)
        if verdict.rejected:
            self.rejected = True

    def result(self) -> Result:
        return Result(
            cls=self.cls,
            reasons=tuple(escape(r) for r in self.reasons),
            rejected=self.rejected,
        )


def classify(command: str, *, origin: str = "typed", label: str | None = None) -> Result:
    """Class ``command`` (spec ``policy-gate/v2`` §1). ``origin`` is ``proposal`` (from
    the model) or ``typed`` (from the user); ``label`` is the model's risk label, if
    any. Never raises: an internal error is a rejected ``privileged`` result."""
    try:
        return _classify(command, origin, label)
    except Exception as exc:  # fail closed
        return Result(
            cls="privileged",
            reasons=("could not class", f"internal error ({type(exc).__name__})"),
            rejected=True,
        )


def _classify(command: str, origin: str, label: str | None) -> Result:
    if not isinstance(command, str) or origin not in ORIGINS:
        raise ValueError("bad arguments")
    if label is not None and label not in CLASSES:
        raise ValueError("unknown model label")
    proposal = origin == "proposal"
    tally = _Tally()

    # §1.1 characters. Quoted or not (stricter than the spec's "outside quotes").
    if render.has_hidden(command):
        if proposal:
            tally.reject("proposal holds a hidden character")
        else:
            tally.raise_to("mutating", "hidden character (shown escaped)")

    # §4, §5: the text scan.
    scanned = scan.scan(command, proposal=proposal)
    for reason in scanned.rejections:
        tally.reject(reason)
    if scanned.privileged:
        for reason in scanned.reasons:
            tally.raise_to("privileged", reason)

    # §2, §3, §7, §8: the only path to read-only.
    recognised = grammar.recognise(command)
    if recognised is None:
        tally.raise_to("mutating", OUTSIDE_GRAMMAR)
    else:
        for invocation in recognised.invocations:
            if any(parse.has_device_socket(w.text) for w in invocation.words) or any(
                r.device_socket for r in invocation.redirects
            ):
                tally.raise_to("mutating", DEVICE_SOCKET)
            verdict = rules.classify(invocation)
            verdict = live.resolve(invocation, verdict)
            tally.verdict(verdict)
            for step in (sensitive.check, words.check):
                extra = step(invocation)
                if extra is not None:
                    tally.verdict(extra)

    # §6: advice may only raise.
    for cls, reason in advice.advise(parse.shell_layer(command)):
        tally.raise_to(cls, reason)

    # §1.7: the model's label raises, never lowers.
    if label is not None and RANK[label] > RANK[tally.cls]:
        tally.raise_to(label, f"model labelled it {label}")
    return tally.result()
