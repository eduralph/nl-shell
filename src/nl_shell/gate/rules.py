"""The policy gate's command layer: a stub (issue #196).

Spec ``policy-gate/v2`` §3: the allowlist and the per-command rules, for each simple
command the read-only grammar produced (ADR-0027). #198 (the allowlist, the file-operand
and terminal-read rules) replaces this stub; the destructive and wrapper rules that #197
planned live in ``nl_shell.gate.advice`` now, as advice. Until then every simple command
is ``mutating``, "not allowlisted", so nothing is ``read-only``.
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_ALLOWLISTED = "not allowlisted"


@dataclass(frozen=True)
class Verdict:
    """One step's answer for one simple command."""

    cls: str
    reasons: tuple[str, ...] = ()
    rejected: bool = False


def classify(invocation) -> Verdict:
    """Class one ``nl_shell.gate.parse.Invocation``."""
    return Verdict("mutating", (NOT_ALLOWLISTED,))
