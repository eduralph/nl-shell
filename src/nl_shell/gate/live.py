"""Live-session resolution (spec ``policy-gate/v2`` §7): a fail-closed stub (issue #196).

#192 replaces it with the real checks against the session snapshot. Until then every
simple command still ``read-only`` after the command layer becomes ``mutating``, with
``NOT_IMPLEMENTED`` as the reason. #198 and #192 assert on that exact string.
"""

from __future__ import annotations

from nl_shell.gate.rules import Verdict

NOT_IMPLEMENTED = "live-session resolution not implemented"


def resolve(invocation, verdict: Verdict) -> Verdict:
    """§7 for one simple command and the verdict the command layer gave it. An alias
    expansion must itself be re-checked against the grammar (§7.1)."""
    if verdict.cls == "read-only":
        return Verdict("mutating", (*verdict.reasons, NOT_IMPLEMENTED), verdict.rejected)
    return verdict
