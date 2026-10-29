"""The text scan (ADR-0027 point 4, spec ``policy-gate/v2`` §4 and §5).

Two things must be decided for *every* command, whatever the parser makes of it: what
is rejected outright (it can't be confirmed), and whether it is ``privileged``. Both are
decided here, by patterns over the raw text with single-quoted runs blanked out, and both
over-approximate: anything that *could* be the construct counts. A false positive costs
the user a rewrite of a rare command; a false negative would reach the control channel
or the password prompt. The scan is deliberately crude so that it has nothing to get
wrong, and it never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PRIVILEGED_WORDS = ("sudo", "doas", "su", "pkexec", "run0", "machinectl")
ROOT_SHELLS = ("bash", "sh", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "su", "-i", "-s")
# sudo options that take a value (sudo 1.9 / sudo-rs); the value is never an option.
_SUDO_VALUE_OPTIONS = frozenset("CDghprtTuU")
_SUDO_REJECT_SHORT = frozenset("SAp")
_SUDO_REJECT_LONG = ("--stdin", "--askpass", "--prompt")
_SUDO_SHELL_SHORT = frozenset("si")
_SUDO_SHELL_LONG = ("--shell", "--login")

_WORD_START = r"(?:^|[\s;|&(){}`$\"\\])"
_WORD_END = r"(?=$|[\s;|&(){}`<>\"])"
_PRIVILEGED = re.compile(
    _WORD_START + r"(?:[^\s;|&(){}`'\"]*/)?(" + "|".join(PRIVILEGED_WORDS) + r")" + _WORD_END
)
# A descriptor written against an operator: `3>`, `12<`, `{fd}>`, `3>&-`.
_FD_BEFORE = re.compile(r"(?<![\w.{\-])(\d+)(?=[<>])")
_VAR_FD_BEFORE = re.compile(r"\{[A-Za-z_][^}\s]*\}(?=[<>])")
# A descriptor as the target: `>&3`, `<& 3`, `>&3-`.
_FD_AFTER = re.compile(r"[<>]&\s*[\"]?(\d+)")
# An fd path, or a bare number or `fd/N` path as a target (the cwd may be `/dev/fd`).
_FD_PATH = re.compile(r"(?:/dev/fd|/proc/[^/\s]+/fd)/(\d+)")
_NUMBER_TARGET = re.compile(r"[<>]\s*[\"]?(\d+)" + _WORD_END)
_FD_DIR_TARGET = re.compile(r"[<>]\s*[\"]?[^\s<>&|;\"]*fd/(\d+)" + _WORD_END)
_SUDO_ENV = re.compile(r"SUDO_(?:PROMPT|ASKPASS)")
_HEREDOC = re.compile(r"<<")
# A declaration whose operand bash names only after expansion (`export "$x"`).
_DECLARED_UNREAD = re.compile(
    r"(?:^|[\s;|&(){}])(?:export|declare|typeset|readonly|local)\s[^;|&\n]*[$`]"
)
_SQ_RUN = re.compile(r"'[^']*'")


@dataclass(frozen=True)
class Scan:
    rejections: tuple[str, ...]  # reasons; the command cannot be confirmed
    privileged: bool
    reasons: tuple[str, ...]  # reasons for `privileged`


def scan(source: str, *, proposal: bool) -> Scan:
    """Scan ``source``. ``proposal`` selects the rules that apply to model proposals only
    (root shells)."""
    try:
        return _scan(source, proposal)
    except Exception:  # never raise; the worst case is to reject
        return Scan(("scan failed",), True, ("scan failed",))


def dequote(source: str) -> str:
    """``source`` with every quote character and backslash removed: what a word made only
    of literal pieces reads as after quote removal (over-approximate for anything else)."""
    return source.replace("'", "").replace('"', "").replace("\\", "")


def blank_single_quotes(source: str) -> str:
    """``source`` with the inside of every closed single-quoted run replaced by spaces.
    An unclosed quote stays as written (over-approximation)."""
    return _SQ_RUN.sub(lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", source)


def _scan(source: str, proposal: bool) -> Scan:
    # Blanking keeps the length, so offsets into `text` are offsets into `raw`. A
    # backslash-newline outside single quotes joins lines for bash (`>\\\n&3` is `>&3`),
    # so it is cut out of both strings at the same positions.
    text = blank_single_quotes(source)
    raw = source
    cut = text.find("\\\n")
    while cut >= 0:
        text = text[:cut] + text[cut + 2 :]
        raw = raw[:cut] + raw[cut + 2 :]
        cut = text.find("\\\n")
    rejections: list[str] = []
    reasons: list[str] = []

    def reject(reason: str) -> None:
        if reason not in rejections:
            rejections.append(reason)

    # §5: descriptors 3 and above, in any spelling the gate can't rule out.
    for match in _FD_BEFORE.finditer(text):
        if int(match.group(1)) >= 3:
            reject(f"redirection involving descriptor {match.group(1)}")
    if _VAR_FD_BEFORE.search(text):
        reject("redirection with a {varname} descriptor")
    for pattern in (_FD_AFTER, _FD_PATH, _NUMBER_TARGET, _FD_DIR_TARGET):
        for match in pattern.finditer(text):
            if int(match.group(1)) >= 3:
                reject(f"redirection involving descriptor {match.group(1)}")
    if _SUDO_ENV.search(dequote(raw)):
        reject("names SUDO_PROMPT or SUDO_ASKPASS")
    if _DECLARED_UNREAD.search(text):
        reject("declaration with an operand the gate cannot name")

    privileged = False
    heredoc = _HEREDOC.search(text) is not None
    for match in _PRIVILEGED.finditer(text):
        privileged = True
        word = match.group(1)
        reason = f"{word} is privileged"
        if reason not in reasons:
            reasons.append(reason)
        before = text[: match.start(1)].rstrip()
        if before.endswith("|") and not before.endswith("||"):
            reject(f"a pipe into {word}")
        if heredoc:
            reject(f"a here-document or here-string with {word}")
        # The options are read from the source with its quotes removed, as bash reads
        # them: `sudo '-S'` is `sudo -S`. Blanking is the same length as the source.
        rest = dequote(raw[match.end(1) :])
        if word in ("sudo", "doas"):
            _sudo_options(word, rest, reject, proposal, heredoc or "|" in text)
        elif proposal and word == "su" and not re.search(r"(?:^|\s)-c(?:\s|$)", rest):
            reject("a proposal opening a root shell (su)")
        elif proposal and word == "pkexec" and _inner_is_shell(rest):
            reject("a proposal opening a root shell (pkexec)")
    return Scan(tuple(rejections), privileged, tuple(reasons))


def _tokens(rest: str) -> list[str]:
    out = []
    for token in re.split(r"[ \t]+", rest.strip(" \t")):
        if not token:
            continue
        # Stop at the end of this simple command.
        cut = re.split(r"[;|&<>()]", token, maxsplit=1)[0]
        if cut != token:
            if cut:
                out.append(cut)
            break
        out.append(token)
    return out


def _inner_is_shell(rest: str) -> bool:
    tokens = _tokens(rest)
    while tokens and tokens[0].startswith("-") and tokens[0] not in ("-i", "-s"):
        tokens.pop(0)
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] in ROOT_SHELLS


def _sudo_options(word: str, rest: str, reject, proposal: bool, piped: bool) -> None:
    """Walk the options between ``sudo`` and its inner command (§5)."""
    tokens = _tokens(rest)
    shell = False
    inner: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "--":
            inner = tokens[i] if i < len(tokens) else None
            break
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _SUDO_REJECT_LONG:
                reject(f"{word} {name}")
            if name in _SUDO_SHELL_LONG:
                shell = True
            if name == "--prompt" and "=" not in token and i < len(tokens):
                i += 1
            continue
        if "$" in token or "`" in token:
            reject(f"{word} with an argument the gate cannot read")
            break
        if token.startswith("-") and len(token) > 1:
            letters = token[1:]
            for n, letter in enumerate(letters):
                if letter in _SUDO_REJECT_SHORT:
                    reject(f"{word} -{letter}")
                if letter in _SUDO_SHELL_SHORT:
                    shell = True
                if letter in _SUDO_VALUE_OPTIONS:
                    if n == len(letters) - 1:
                        i += 1  # the value is the next token
                    break  # the rest of the cluster is the value
            continue
        inner = token
        break
    if shell and piped:
        reject(f"{word} -s or -i with a pipe or here-document")
    if proposal and (shell or (inner is not None and inner.rsplit("/", 1)[-1] in ROOT_SHELLS)):
        reject(f"a proposal opening a root shell ({word})")
