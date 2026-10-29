"""Advisory labelling (spec ``policy-gate/v2`` §6): what the tree-sitter walk can say
about a command that is already going to be confirmed.

``advise`` turns a ``ShellLayer`` into ``(class, reason)`` pairs that the gate uses to
*raise* the class above ``mutating`` and to word the confirm: destructive commands,
session-altering builtins and assignments, prefix assignments, output redirections,
function definitions, process substitution, and the inner command of a wrapper
(``env``, ``nice``, ``xargs``, ``find -exec``, ``bash -c``, ``sudo`` …). Nothing here can
lower a class or grant ``read-only``, and nothing here rejects; a layer that was not
analysed gives no advice at all.
"""

from __future__ import annotations

import re

from nl_shell.gate.parse import ShellLayer
from nl_shell.gate.types import Invocation, Word

Advice = list[tuple[str, str]]

# §3: builtins that change the session, and names whose assignment does.
SESSION_BUILTINS = frozenset(
    (
        "alias", "unalias", "source", ".", "eval", "exec", "trap", "enable", "set",
        "shopt", "bind", "complete", "hash", "builtin", "export", "declare", "typeset",
        "local", "readonly", "unset",
    )
)  # fmt: skip
SESSION_NAMES = frozenset(
    (
        "PATH", "BASH_ENV", "ENV", "CDPATH", "PROMPT_COMMAND", "PS0", "PS1", "PS2", "PS3",
        "PS4", "IFS", "HISTFILE", "PAGER", "MANPAGER", "SYSTEMD_PAGER", "GIT_PAGER",
        "LESSOPEN", "LESSCLOSE", "EDITOR", "VISUAL", "BROWSER", "NODE_OPTIONS",
    )
)  # fmt: skip
SESSION_PREFIXES = ("LD_", "SUDO_", "PYTHON", "PERL5", "GIT_")

# §4: wrappers whose inner command is classed too: for each, the short options that take
# a value (so the value is not read as the inner command) and how many positional words
# precede the inner command (`timeout DURATION cmd`).
WRAPPERS: dict[str, tuple[str, int]] = {
    "env": ("uCS", 0),
    "nice": ("n", 0),
    "nohup": ("", 0),
    "timeout": ("sk", 1),
    "time": ("f", 0),
    "xargs": ("nIPdaELsR", 0),
    "watch": ("nd", 0),
    "sudo": ("CDghprtTuU", 0),
    "doas": ("Cu", 0),
    "command": ("", 0),
    "builtin": ("", 0),
}
SHELL_C = ("bash", "sh", "dash", "zsh")
PRIVILEGED = frozenset(("sudo", "su", "pkexec", "doas", "run0", "machinectl"))
_DESTRUCTIVE_WORDS = frozenset(("dd", "wipefs", "shred", "truncate", "fdisk", "sfdisk",
                                "parted", "gdisk"))  # fmt: skip
_SYSTEMCTL_DESTRUCTIVE = frozenset(("disable", "mask", "stop", "kill", "isolate", "poweroff",
                                    "reboot", "halt"))  # fmt: skip


def advise(layer: ShellLayer) -> Advice:
    if not layer.analysed:
        return []
    advice: Advice = []

    def add(cls: str, reason: str) -> None:
        if (cls, reason) not in advice:
            advice.append((cls, reason))

    for name in layer.functions:
        add("session-altering", f"function definition: {name}")
    if layer.process_substitutions:
        add("mutating", "process substitution")
    for record in layer.redirects:
        if record.device_socket:
            add("mutating", "uses /dev/tcp or /dev/udp")
        if record.output and not record.exempt:
            add("mutating", f"output redirection: {record.display}")
    for assignment in layer.assignments:
        name = assignment.name
        if not name:
            add("mutating", "declaration with an operand the gate cannot name")
        elif name in SESSION_NAMES or name.startswith(SESSION_PREFIXES):
            add("session-altering", f"assignment to {name}")
    for invocation in layer.invocations:
        for assignment in invocation.assignments:
            if assignment.role == "prefix":
                add("mutating", f"prefix assignment: {assignment.name}")
        for cls, reason in _command(list(invocation.words)):
            add(cls, reason)
    return advice


def _command(words: list[Word]) -> Advice:
    """§4 for one simple command, looking through wrappers."""
    advice: Advice = []
    depth = 0
    while words and depth < 8:
        depth += 1
        head = words[0]
        if not head.literal or not head.source:
            if head.source:
                advice.append(("mutating", f"command word is not a literal: {head.source}"))
            return advice
        name = head.text.rsplit("/", 1)[-1] if "/" in head.text else head.text
        args = [w.text for w in words[1:]]
        if name in PRIVILEGED:
            advice.append(("privileged", f"{name} is privileged"))
            if name not in ("sudo", "doas"):
                return advice
            words = _after_options(words[1:], *WRAPPERS[name])
            continue
        if name in SESSION_BUILTINS:
            advice.append(("session-altering", f"session-altering builtin: {name}"))
            if name in ("command", "builtin"):
                words = _after_options(words[1:], *WRAPPERS[name])
                continue
            return advice
        destructive = _destructive(name, args)
        if destructive:
            advice.append(("destructive", destructive))
            return advice
        if name in WRAPPERS:
            advice.append(("mutating", f"{name} runs another command"))
            words = _after_options(words[1:], *WRAPPERS[name], env=name == "env")
            continue
        if name in SHELL_C and "-c" in args:
            advice.append(("mutating", f"{name} -c runs a command string"))
            return advice
        if name == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
            inner = next((i for i, a in enumerate(args) if a.startswith("-exec") or a.startswith("-ok")), None)
            advice.append(("mutating", "find runs a command"))
            if inner is not None and inner + 1 < len(words) - 1:
                words = words[inner + 2 :]
                continue
            return advice
        return advice
    return advice


def _after_options(
    words: list[Word], value_options: str = "", positional: int = 0, *, env: bool = False
) -> list[Word]:
    """Drop a wrapper's own options (with their values), ``env``'s assignments and the
    wrapper's positional words to reach its inner command."""
    out = list(words)
    while out and out[0].literal:
        text = out[0].text
        if text == "--":
            out.pop(0)
            break
        if text.startswith("--") and len(text) > 2:
            out.pop(0)
            if "=" not in text and out and not out[0].text.startswith("-") and positional == 0:
                # `--option value`: over-approximate by keeping the next word as the
                # inner command only when it does not look like a value for a known
                # value option; long options are too many to list, so leave it.
                pass
            continue
        if text.startswith("-") and len(text) > 1:
            out.pop(0)
            if text[-1] in value_options and len(text) >= 2 and out:
                out.pop(0)  # the value is the next word
            continue
        if env and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", text, re.S):
            out.pop(0)
            continue
        break
    for _ in range(positional):
        if out:
            out.pop(0)
    return out


def _destructive(name: str, args: list[str]) -> str | None:
    flags = " ".join(args)
    if name == "rm" and (
        any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", a) or a == "--recursive" for a in args)
        or any(c in a for a in args for c in "*?[")
    ):
        return "rm -r or rm with a glob"
    if name in _DESTRUCTIVE_WORDS or name.startswith("mkfs"):
        return f"{name} is destructive"
    if name in ("chmod", "chown") and any(re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", a) for a in args):
        return f"{name} -R"
    if name == "find" and "-delete" in args:
        return "find -delete"
    if name == "git":
        if "clean" in args:
            return "git clean"
        if "reset" in args and "--hard" in args:
            return "git reset --hard"
        if "push" in args and ("--force" in args or "-f" in args or "--force-with-lease" in args):
            return "git push --force"
    if name == "systemctl" and any(a in _SYSTEMCTL_DESTRUCTIVE for a in args):
        verb = next(a for a in args if a in _SYSTEMCTL_DESTRUCTIVE)
        return f"systemctl {verb}"
    if name == "kill" and "-9" in flags and "-1" in args:
        return "kill -9 -1"
    return None
