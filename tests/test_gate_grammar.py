"""The read-only grammar (ADR-0027, spec ``policy-gate/v2`` §2).

Two kinds of test: the shape of the grammar (what is in and what is out), and the one
property the gate's safety rests on — for every string the recogniser accepts, the words
it produces are the words bash runs. That property is checked against bash itself: the
command words are shell functions that record their arguments, and the recogniser's
split must match the recording. There is no second parser to differ from.
"""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

import pytest

RS, US = "\x1e", "\x1f"


def grammar():
    from nl_shell.gate import grammar

    return grammar


def argv(text):
    return grammar().argv(text)


# --- shape ---------------------------------------------------------------------------

ACCEPTED = {
    "ls": [["ls"]],
    "ls -la /etc": [["ls", "-la", "/etc"]],
    "ls\t-la": [["ls", "-la"]],
    "  ls  ": [["ls"]],
    "ls;": [["ls"]],
    "ls ; pwd": [["ls"], ["pwd"]],
    "ls && pwd || id": [["ls"], ["pwd"], ["id"]],
    "ls | grep x | wc -l": [["ls"], ["grep", "x"], ["wc", "-l"]],
    "grep -i 'x y' f": [["grep", "-i", "x y", "f"]],
    'grep "a b" f': [["grep", "a b", "f"]],
    "grep a'b'c\"d\" f": [["grep", "abcd", "f"]],
    "--x='y z'": None,  # not a command word (`=`)
    "grep --include='*.py' x": [["grep", "--include=*.py", "x"]],
    "echo '' \"\"": [["echo", "", ""]],
    "'ls' -l": [["ls", "-l"]],
    "cat < file": [["cat"]],
    "cat <file": [["cat"]],
    "cat <'x y'": [["cat"]],
    "ls 2>&1": [["ls"]],
    "ls >/dev/null": [["ls"]],
    "ls > /dev/null": [["ls"]],
    "ls 2>/dev/null": [["ls"]],
    "ls &>/dev/null": [["ls"]],
    "ls 2>&1 | grep x": [["ls"], ["grep", "x"]],
    "echo 2 >/dev/null": [["echo", "2"]],
    "echo '>&3'": [["echo", ">&3"]],
    "echo \"2>&1\"": [["echo", "2>&1"]],
    "grep '$HOME' f": [["grep", "$HOME", "f"]],
    "grep 'a\"b' f": [["grep", 'a"b', "f"]],
    "grep \"a'b\" f": [["grep", "a'b", "f"]],
    "ls a=b": [["ls", "a=b"]],
    "ls /dev/tcp/x/80": [["ls", "/dev/tcp/x/80"]],
}

OUTSIDE = [
    "",
    " ",
    "ls ~",
    "du -sh ~/*",
    "ls *.py",
    "ls $HOME",
    "ls `pwd`",
    "ls $(pwd)",
    "echo {a,b}",
    "ls #c",
    "ls\npwd",
    "ls &",
    "ls |& wc",
    "ls ;;",
    "; ls",
    "| ls",
    "ls |",
    "ls &&",
    "ls || ",
    "ls > out",
    "ls >> /dev/null",
    "ls 2>&2",
    "ls 22>&1",
    "ls 3>&1",
    "ls 1>&2",
    "ls >&2",
    "ls 2> /dev/nul",
    "cat << E",
    "cat <<< x",
    "diff <(ls) <(ls)",
    "x=1 ls",
    "PATH=x",
    "ls 'unterminated",
    'ls "unterminated',
    'ls "$x"',
    'ls "`x`"',
    'ls "a\\"b"',
    'ls "a!b"',
    "ls 'a\nb'",
    "if true; then ls; fi",
    "time ls",
    "! ls",
    "{ ls; }",
    "(ls)",
    "f() { ls; }",
    "[[ -f x ]]",
    "[ -f x ]",
    "'' ls",
    "ls a2>&1",
    "ls \\-l",
    "ls -l\\",
    "ls ‮x",
    "ls ä",
    "x" * 9000,
]


@pytest.mark.parametrize("text", list(ACCEPTED), ids=lambda t: repr(t)[:40])
def test_accepted(text):
    expected = ACCEPTED[text]
    assert argv(text) == expected


@pytest.mark.parametrize("text", OUTSIDE, ids=lambda t: repr(t)[:40])
def test_outside(text):
    assert grammar().recognise(text) is None


def test_quoted_words_may_hold_non_ascii_and_tabs():
    assert argv("grep 'ä\tö' f") == [["grep", "ä\tö", "f"]]
    assert argv('grep "ä" f') == [["grep", "ä", "f"]]


def test_reserved_words_only_as_a_single_bare_piece():
    assert grammar().recognise("time ls") is None
    assert grammar().recognise("in") is None
    assert argv("'time' ls") == [["time", "ls"]]
    assert argv("ls time") == [["ls", "time"]]


def test_invocations_carry_stdin_and_redirects():
    found = grammar().recognise("cat < f | grep x 2>&1 >/dev/null; ls")
    kinds = [(i.command_word.text, i.stdin.kind, i.stdin.value) for i in found.invocations]
    assert kinds == [("cat", "file", "f"), ("grep", "pipe", None), ("ls", "terminal", None)]
    grep = found.invocations[1]
    assert [r.display for r in grep.redirects] == ["2>&1", ">/dev/null"]
    assert all(r.exempt and r.output for r in grep.redirects)
    assert found.invocations[0].redirects[0].output is False
    assert all(w.literal for i in found.invocations for w in i.words)
    assert all(i.assignments == () for i in found.invocations)


def test_device_socket_flag_on_input_redirect():
    found = grammar().recognise("cat </dev/tcp/example.org/80")
    assert found.invocations[0].redirects[0].device_socket


def test_never_raises_on_garbage():
    for text in (None, 1, b"ls", "\x00", "\udcff", "'" * 5000, "<" * 5000):
        assert grammar().recognise(text) is None  # type: ignore[arg-type]


# --- the bash oracle -----------------------------------------------------------------

COMMAND_WORDS = ["p", "q", "'p'", '"q"', "p''", "p\"\""]
OPERANDS = [
    "a", "-n", "--x=y", "a/b.c", "1", "2", "22", "a2", "x=y", "@h:%", "+,.",
    "'x y'", "'$HOME'", "'a\"b'", "'>&3'", "''", "'2>&1'", "'|'",
    '"q r"', "\"a'b\"", '""', '"2>&1"', '"a;b"',
    "a'b'c", "--x='y z'", '"a"b', "'a'\"b\"", "a\"\"b", "x''y",
]  # fmt: skip
REDIRECTS = ["2>&1", ">/dev/null", "> /dev/null", "2>/dev/null", "&>/dev/null", "<a", "< a", "<'x y'"]
# `||` is left out: its right-hand side does not run when the left succeeds.
OPERATORS = [" | ", "|", " ; ", ";", " && ", "&&", "\t|\t"]

# One printf, one write per record: pipeline stages run concurrently, and only a single
# O_APPEND write is atomic.
RC = """
__rec() { local IFS=$'\\x1f'; printf '%s\\x1e' "$*" >&9; }
p() { __rec p "$@"; }
q() { __rec q "$@"; }
"""


def bash_words(text: str, cwd: Path) -> tuple[int, list[list[str]]]:
    out = cwd / "out"
    out.write_bytes(b"")
    script = f"exec 9>>{out.name}\n{RC}\n{text}\n"
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
        timeout=10,
    )
    records = out.read_bytes().decode("utf-8").split(RS)
    found = [r.split(US) for r in records if r]
    return proc.returncode, found


def generate(rng: random.Random) -> str:
    parts = []
    for n in range(rng.randint(1, 4)):
        if n:
            parts.append(rng.choice(OPERATORS))
        words = [rng.choice(COMMAND_WORDS)]
        words += [rng.choice(OPERANDS) for _ in range(rng.randint(0, 4))]
        redirects = [rng.choice(REDIRECTS) for _ in range(rng.randint(0, 2))]
        for r in redirects:
            words.insert(rng.randint(1, len(words)), r)
        parts.append(rng.choice([" ", "  ", "\t"]).join(words))
    text = "".join(parts)
    if rng.random() < 0.2:
        text += rng.choice([";", " ;", " "])
    return text


def test_bash_oracle(tmp_path):
    """For every accepted string, bash runs exactly the words the recogniser produced,
    and runs without a syntax error."""
    (tmp_path / "a").write_text("")
    (tmp_path / "x y").write_text("")
    rng = random.Random(20261028)
    checked = 0
    for _ in range(400):
        text = generate(rng)
        ours = argv(text)
        if ours is None:
            continue
        status, theirs = bash_words(text, tmp_path)
        assert status == 0, (text, status)
        assert sorted(theirs) == sorted(ours), text
        checked += 1
    assert checked > 150, checked


@pytest.mark.parametrize(
    "text",
    [
        "p 2>&1",
        "p a2>&1",
        "p 22>&1",
        "p '2>&1'",
        "p \"2>&1\"",
        "p 2 >/dev/null",
        "p ''2>&1",
        "p 'a'2>&1",
        "p<a",
        "p <a|q",
        "p;q",
        "p&&q",
        "p|q 2>&1;q",
        "p a= =b",
        "p --x=='y'",
    ],
)
def test_bash_oracle_edges(tmp_path, text):
    (tmp_path / "a").write_text("")
    ours = argv(text)
    if ours is None:
        return  # outside the grammar is always safe
    status, theirs = bash_words(text, tmp_path)
    assert status == 0, text
    assert sorted(theirs) == sorted(ours), text
