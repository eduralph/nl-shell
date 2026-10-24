"""The policy gate's command layer: a stub (issue #196).

Spec ``policy-gate/v1`` §1 step 4: the static, session-altering, destructive and
privileged rules and the allowlist, per simple command. #197 (wrappers, sudo, destructive
commands, builtins, decisions A and B) and #198 (the allowlist, the terminal-read rule,
the here-doc exemption) replace this stub. Until then every simple command is
``mutating``, "not allowlisted": decision A's floor, so nothing is ``read-only``.
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
