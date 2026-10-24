"""Word-level character checks (mixed-script words): a no-op stub (issue #196).

#191 replaces it. The pipeline already calls ``check`` for every simple command; ``None``
means "no change".
"""

from __future__ import annotations

from nl_shell.gate.rules import Verdict


def check(invocation) -> Verdict | None:
    return None
