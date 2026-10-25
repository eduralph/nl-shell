"""The policy gate (ADR-0013, spec ``docs/design/specs/policy-gate/v1.md``).

``classify(command, origin=..., label=...)`` runs the spec's §1 decision procedure and
returns a ``Result`` with the session-log ``gate`` shape
(``{"class", "reasons", "rejected"}``, spec ``session-log/v1``). It never raises.

This package ships the **shell layer** (issue #196): the character check (§1.1), the
parse and walk (``nl_shell.gate.parse``, Rules 1-5), and the shell-level rules of §2, §3
and §5 that need no per-command meaning. The later steps are stubs the pipeline already
calls, for the issues that replace them:

- ``rules`` (the command layer, #197/#198): every simple command is ``mutating``,
  "not allowlisted" (decision A's floor);
- ``live`` (§7, #192): every simple command still ``read-only`` becomes ``mutating``,
  "live-session resolution not implemented";
- ``sensitive`` (§8, #190) and ``words`` (#191): no-ops.

Decision C: any gate failure (an unhandled node kind, the work budget, input over the size
cap, any internal error) gives ``privileged``, rejected, reason "could not class".

Each reason that quotes input escapes it (Rule 6): no reason holds a C0 or C1 control
character or any character ``nl_shell.render.is_hidden`` reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nl_shell import render
from nl_shell.gate import live, parse, rules, sensitive, words
from nl_shell.gate.parse import CLASSES, RANK, Invocation, Stdin, Word
from nl_shell.gate.rules import Verdict

__all__ = [
    "CLASSES",
    "COULD_NOT_CLASS",
    "ORIGINS",
    "Invocation",
    "Result",
    "Stdin",
    "Verdict",
    "Word",
    "classify",
]

ORIGINS = ("proposal", "typed")
COULD_NOT_CLASS = "could not class"

# §3: assignments to these names alter the session.
_SESSION_NAMES = frozenset(
    (
        "PATH",
        "BASH_ENV",
        "ENV",
        "CDPATH",
        "PROMPT_COMMAND",
        "PS0",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "IFS",
        "HISTFILE",
        "PAGER",
        "MANPAGER",
        "SYSTEMD_PAGER",
        "GIT_PAGER",
        "LESSOPEN",
        "LESSCLOSE",
        "EDITOR",
        "VISUAL",
        "BROWSER",
        "NODE_OPTIONS",
    )
)
_SESSION_PREFIXES = ("LD_", "SUDO_", "PYTHON", "PERL5", "GIT_")
_SUDO_REJECTED = ("SUDO_PROMPT", "SUDO_ASKPASS")  # §5
_SYSTEM_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")


@dataclass(frozen=True)
class Result:
    """The gate's answer: the session-log ``gate`` object."""

    cls: str
    reasons: tuple[str, ...]
    rejected: bool

    def as_log(self) -> dict:
        return {"class": self.cls, "reasons": list(self.reasons), "rejected": self.rejected}


def escape(text: str) -> str:
    """``text`` safe to put in a reason (Rule 6): backslashes doubled, and every C0/C1
    control and every hidden character written as an escape."""
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


def _could_not_class(detail: str) -> Result:
    return Result(cls="privileged", reasons=(COULD_NOT_CLASS, detail), rejected=True)


def classify(command: str, *, origin: str = "typed", label: str | None = None) -> Result:
    """Class ``command`` (spec ``policy-gate/v1`` §1). ``origin`` is ``proposal`` (from
    the model) or ``typed`` (from the user); ``label`` is the model's risk label, if
    any. Never raises: a failure is decision C."""
    try:
        return _classify(command, origin, label)
    except parse.GateFailure as exc:
        return _could_not_class(str(exc))
    except Exception as exc:  # decision C: any internal error fails closed
        return _could_not_class(f"internal error ({type(exc).__name__})")


def _classify(command: str, origin: str, label: str | None) -> Result:
    if not isinstance(command, str) or origin not in ORIGINS:
        raise parse.GateFailure("bad arguments")
    if label is not None and label not in CLASSES:
        raise parse.GateFailure("unknown model label")
    proposal = origin == "proposal"
    tally = _Tally()

    # §1.1 characters. Quoted or not (stricter than the spec's "outside quotes").
    if render.has_hidden(command):
        if proposal:
            tally.reject("proposal holds a hidden character")
        else:
            tally.raise_to("mutating", "hidden character (shown escaped)")

    layer = parse.shell_layer(command)  # Rule 5's size cap is checked before parsing

    # §1.2, Rule 1.
    for feature in layer.untrusted:
        if proposal:
            tally.reject(f"unparseable proposal: {feature}")
        else:
            tally.raise_to("mutating", f"unparseable: {feature}")

    _redirects(tally, layer)
    _assignments(tally, layer)
    for name in layer.functions:
        tally.raise_to("session-altering", f"function definition: {name}")
    if layer.process_substitutions:
        tally.raise_to("mutating", "process substitution")
    if any(parse.has_device_socket(w.text) for w in layer.words) or parse.has_device_socket(
        command
    ):
        tally.raise_to("mutating", "uses /dev/tcp or /dev/udp")

    for invocation in layer.invocations:
        _shell_rules(tally, invocation)
        verdict = rules.classify(invocation)
        verdict = live.resolve(invocation, verdict)
        tally.verdict(verdict)
        for step in (sensitive.check, words.check):
            extra = step(invocation)
            if extra is not None:
                tally.verdict(extra)
    if not layer.invocations:
        # Nothing was proven read-only.
        tally.raise_to("mutating", "no simple command to class")

    # §1.7: the model's label raises, never lowers.
    if label is not None and RANK[label] > RANK[tally.cls]:
        tally.raise_to(label, f"model labelled it {label}")
    return tally.result()


def _redirects(tally: _Tally, layer: parse.ShellLayer) -> None:
    for record in layer.redirects:
        shown = record.display
        if not record.accepted:
            tally.reject(f"redirection rejected ({record.why}): {shown}")
        if layer.untrusted and record.kind in ("file_redirect", "loose"):
            # Rule 1: in untrusted text only the §2 exempt forms stand, apart from a
            # here-doc operator (`<<`), which in bash only feeds stdin.
            if not record.exempt and not record.heredoc_operator:
                tally.reject(f"unparseable: redirection in untrusted text: {shown}")
        if record.device_socket:
            tally.raise_to("mutating", "uses /dev/tcp or /dev/udp")
        if record.accepted and record.output and not record.exempt:
            tally.raise_to("mutating", f"output redirection: {shown}")


def _assignments(tally: _Tally, layer: parse.ShellLayer) -> None:
    for assignment in layer.assignments:
        name = assignment.name
        if not name:
            # `export "$x"`: bash assigns whatever name the expansion gives (§5).
            tally.reject("declaration with an operand the gate cannot name")
            continue
        if name in _SUDO_REJECTED:
            tally.reject(f"assignment to {name} is rejected")
        if assignment.role == "prefix" and not layer.untrusted:
            # A prefix assignment lasts for one command (§2, below). In an untrusted
            # tree that is not proven (`PATH=x >/dev/null; cat` parses `cat` as the
            # name), so §3 applies.
            continue
        if name in _SESSION_NAMES or name.startswith(_SESSION_PREFIXES):
            tally.raise_to("session-altering", f"assignment to {name}")


def _shell_rules(tally: _Tally, invocation: Invocation) -> None:
    """§2 rules that need no per-command meaning."""
    for assignment in invocation.assignments:
        if assignment.role == "prefix":
            tally.raise_to("mutating", f"prefix assignment: {assignment.name}")
    word = invocation.command_word
    if word is None or not word.source:
        return  # no command word (`PATH=x >/dev/null`): nothing to judge here
    if not word.literal:
        tally.raise_to("mutating", f"command word is not a literal: {word.source}")
    elif "/" in word.text and not _system_path(word.text):
        tally.raise_to(
            "mutating",
            f"command word is a path outside the system directories: {word.text}",
        )


def _system_path(path: str) -> bool:
    if not path.startswith("/"):
        return False
    parts = path.split("/")
    if ".." in parts or "." in parts:
        return False
    directory, _, name = re.sub(r"/+", "/", path).rpartition("/")
    return bool(name) and directory in _SYSTEM_DIRS
