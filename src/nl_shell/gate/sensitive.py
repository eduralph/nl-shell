"""Sensitive locations (spec ``policy-gate/v1`` §8): a no-op stub (issue #196).

#190 replaces it (``targets/<id>/sensitive.toml``, ADR-0025). The pipeline already calls
``check`` for every simple command; ``None`` means "no change".
"""

from __future__ import annotations

from nl_shell.gate.rules import Verdict


def check(invocation) -> Verdict | None:
    return None
