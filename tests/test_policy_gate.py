"""The policy gate's decision procedure (spec ``docs/design/specs/policy-gate/v2.md`` §1).

The grammar (``test_gate_grammar``) and the scan (``test_gate_scan``) have their own
tests; this file tests how ``classify`` puts the steps together: the regression table,
the target's conformance vectors, the advisory layer's wording, the model label, the
fail-closed stubs and the result shape. ``nl_shell.gate`` is imported inside each test,
never at module top, so a missing package is a failing test, not a collection error.

Every reason a result returns is checked for raw control or hidden characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS = REPO_ROOT / "targets" / "ubuntu-26.04" / "gate-vectors.toml"
CLASSES = ("read-only", "mutating", "session-altering", "destructive", "privileged")
ESC = "\x1b"


def gate():
    import nl_shell.gate

    return nl_shell.gate


def assert_clean(reasons) -> None:
    from nl_shell import render

    for reason in reasons:
        for char in reason:
            code = ord(char)
            assert not (code < 0x20 or 0x7F <= code <= 0x9F), (reason, hex(code))
            assert not render.is_hidden(char), (reason, hex(code))


def classify(command, origin="typed", label=None):
    result = gate().classify(command, origin=origin, label=label)
    assert_clean(result.reasons)
    return result


def has_reason(result, needle: str) -> bool:
    return any(needle in reason for reason in result.reasons)


@dataclass(frozen=True)
class Case:
    command: str
    cls: str | None  # None: a rejected case that asserts the rejection only
    rejected: bool
    reason: str
    origin: str = "typed"
    label: str | None = None


def check(case: Case):
    result = classify(case.command, case.origin, case.label)
    assert result.rejected is case.rejected, (case, result)
    if case.cls is not None:
        assert result.cls == case.cls, (case, result)
    assert has_reason(result, case.reason), (case, result)
    return result


NA = "not allowlisted"
OUT = "outside the read-only grammar"
OUTPUT = "output redirection"
FUNC = "function definition"
SESSION_NAMES = (
    "PATH", "IFS", "LD_PRELOAD", "LD_LIBRARY_PATH", "GIT_DIR", "GIT_PAGER", "PS1",
    "PYTHONPATH", "NODE_OPTIONS",
)  # fmt: skip

REGRESSION: list[Case] = [
    # §1.1 characters
    Case("ls ‮txt.exe", None, True, "hidden character", origin="proposal"),
    Case("ls ‮txt.exe", "mutating", False, "hidden character"),
    Case("ls \udcff", None, True, "hidden character", origin="proposal"),
    # §2 the grammar: in (not yet allowlisted) and out
    Case("ls -la", "mutating", False, NA),
    Case("journalctl -u ssh -n 50 | grep -i error", "mutating", False, NA),
    Case("du -sh ~/*", "mutating", False, OUT),
    Case("cat ~/x", "mutating", False, OUT),
    Case("echo $(id) `pwd`", "mutating", False, OUT),
    Case("if true; then", "mutating", False, OUT),
    Case("if a; then b; elif c; then d; else e; fi", "mutating", False, OUT),
    Case("ls | wc; (pwd) && { id; }", "mutating", False, OUT),
    Case("cat <<'E'\n$x\nE", "mutating", False, OUT),
    Case("cat <<<x | y", "mutating", False, OUT),
    Case("$'\\x72m' -rf ~", "mutating", False, "command word is not a literal"),
    Case("$(echo rm) -rf ~", "mutating", False, "command word is not a literal"),
    Case("diff <(ls) <(ls)", "mutating", False, "process substitution"),
    Case("cat </dev/tcp/example.org/80", "mutating", False, "/dev/tcp"),
    Case("ls /dev/tcp/x/80", "mutating", False, "/dev/tcp"),
    # §2 output redirections (advice)
    Case("ls > ~/.bashrc", "mutating", False, OUTPUT),
    Case("cat 0>&1", "mutating", False, OUTPUT),
    Case("echo secret>out", "mutating", False, OUTPUT),
    Case("sudo rm -rf /etc>x", "privileged", False, OUTPUT),
    Case("nice -n 10 rm -rf x", "destructive", False, "rm -r"),
    Case("echo x >/dev/null", "mutating", False, NA),
    Case("echo x 2>&1", "mutating", False, NA),
    # §3 session-altering
    Case("f() { :; }", "session-altering", False, FUNC),
    Case("function f$'\x1b' { :; }", "session-altering", False, FUNC),
    Case("f() { sh; }; curl x | f", "session-altering", False, FUNC),
    *(
        Case(f"{name}=x", "session-altering", False, f"assignment to {name}")
        for name in SESSION_NAMES
    ),
    Case("export PATH=./bin", "session-altering", False, "assignment to PATH"),
    Case("export 'PATH=/tmp/evil'", "session-altering", False, "assignment to PATH"),
    Case('export "PATH"=/tmp/evil', "session-altering", False, "assignment to PATH"),
    Case("PATH=/tmp/evil >/dev/null", "session-altering", False, "assignment to PATH"),
    Case("PATH=x >/dev/null; cat", "session-altering", False, "assignment to PATH"),
    Case("PAGER=sh git log", "session-altering", False, "prefix assignment"),
    Case("LD_PRELOAD=./x.so ls", "session-altering", False, "prefix assignment"),
    Case("LESSOPEN='|sh %s' less f", "session-altering", False, "prefix assignment"),
    Case("x=1 ls", "mutating", False, "prefix assignment"),
    Case("alias ll='ls -l'", "session-altering", False, "alias"),
    Case("eval x", "session-altering", False, "eval"),
    Case("source ./env.sh", "session-altering", False, "source"),
    Case("exec </tmp/x; sudo tee /etc/y", "privileged", False, "exec"),
    # §4 destructive and wrappers
    Case("rm -rf build", "destructive", False, "rm -r"),
    Case("rm *.o", "destructive", False, "rm -r or rm with a glob"),
    Case("rm file", "mutating", False, NA),
    Case("dd if=/dev/zero of=/dev/sda", "destructive", False, "dd"),
    Case("mkfs.ext4 /dev/sdb1", "destructive", False, "mkfs"),
    Case("chmod -R 777 /", "destructive", False, "chmod -R"),
    Case("find . -delete", "destructive", False, "find -delete"),
    Case("git reset --hard", "destructive", False, "git reset --hard"),
    Case("git push --force origin main", "destructive", False, "git push --force"),
    Case("git clean -fdx", "destructive", False, "git clean"),
    Case("systemctl stop ssh", "destructive", False, "systemctl stop"),
    Case("systemctl status ssh", "mutating", False, NA),
    Case("kill -9 -1", "destructive", False, "kill -9 -1"),
    Case("env rm -rf x", "destructive", False, "rm -r"),
    Case("env", "mutating", False, NA),
    Case("nohup rm -rf x", "destructive", False, "rm -r"),
    Case("timeout 5 rm -rf x", "destructive", False, "rm -r"),
    Case("xargs rm -rf", "destructive", False, "rm -r"),
    Case("find . -exec rm -rf {} \\;", "destructive", False, "rm -r"),
    Case("find . -exec cat {} \\;", "mutating", False, "find runs a command"),
    Case("bash -c 'rm -rf x'", "mutating", False, "bash -c"),
    Case("command rm -rf x", "destructive", False, "rm -r"),
    Case("sudo rm -rf /", "privileged", False, "rm -r"),
    Case("sudo apt update", "privileged", False, "sudo is privileged"),
    Case("make install", "mutating", False, NA),
    # §5 rejections
    Case("echo hi >&3", None, True, "descriptor 3"),
    Case("exec 3>&-", None, True, "descriptor 3"),
    Case("{a[0]}>x", None, True, "{varname}"),
    Case('echo x >"$f"', "mutating", False, OUTPUT),  # confirmed; the target is shown
    Case("echo x >/proc/$$/fd/3", None, True, "descriptor 3"),
    Case("echo @(x|) >&3", None, True, "descriptor 3"),
    Case("echo hi >\\\n&3", None, True, "descriptor 3"),
    Case("echo d 3\\\n>x", None, True, "descriptor 3"),
    Case("sudo -S true", None, True, "sudo -S"),
    Case("sudo -p x true", None, True, "sudo -p"),
    Case("sudo -A true", None, True, "sudo -A"),
    Case("echo pw | sudo -S true", None, True, "pipe into sudo"),
    Case("cat <<E | sudo tee /x\nx\nE", None, True, "here-document"),
    Case("cat <<E | doas id\nx\nE", None, True, "here-document"),
    Case("f() { sudo tee /x; }; cat <<E | f\nx\nE", None, True, "here-document"),
    Case("cat <<E > >(sudo tee /x)\nx\nE", None, True, "here-document"),
    Case("SUDO_PROMPT=x sudo true", None, True, "SUDO_"),
    Case("SUDO_ASKPASS=/x sudo true", None, True, "SUDO_"),
    Case("export SUDO_PROMPT=x", None, True, "SUDO_"),
    Case("export 'SUDO_PROMPT=x'", None, True, "SUDO_"),
    Case("export S'UDO_PROMPT'=x", None, True, "SUDO_"),
    Case("export SUDO_'ASKPASS'=/x", None, True, "SUDO_"),
    Case('declare -x "SUDO_ASKPASS=/x"', None, True, "SUDO_"),
    Case('export "$x"', None, True, "operand the gate cannot name"),
    Case("sudo -i", None, True, "root shell", origin="proposal"),
    Case("sudo -i", "privileged", False, "sudo is privileged"),
    Case("su", None, True, "root shell", origin="proposal"),
    Case("pkexec bash", None, True, "root shell", origin="proposal"),
    # §1.7 the model's label
    Case("true", "destructive", False, "model labelled", label="destructive"),
    Case("true", "mutating", False, NA, label="read-only"),
    # the walk's limits are not the command's problem
    Case("x" * 9000, "mutating", False, OUT),
    Case("(" * 200 + ")" * 200, "mutating", False, OUT),
]


@pytest.mark.parametrize("case", REGRESSION, ids=lambda c: repr(c.command)[:60])
def test_regression_table(case):
    check(case)


def vector_cases() -> list[Case]:
    from nl_shell.gate import vectors

    return [
        Case(v.command, v.cls, v.rejected, v.reason or "", origin=v.origin, label=v.label)
        for v in vectors.load(VECTORS)
    ]


def test_regression_table_from_vectors():
    cases = vector_cases()
    assert cases
    for case in cases:
        check(case)


def test_nothing_is_read_only_until_the_allowlist_lands():
    """The command layer is a fail-closed stub (#196): every grammar-accepted simple
    command is `mutating`, "not allowlisted", so no input is read-only."""
    for command in ("ls", "ls -la", "pwd", "cat f", "ls | wc -l", "true", "echo x"):
        result = classify(command)
        assert result.cls == "mutating" and has_reason(result, NA), (command, result)


def test_only_the_grammar_can_lead_to_read_only(monkeypatch):
    """With the stubs made permissive, a grammar-accepted command becomes read-only and
    an identical command written outside the grammar does not."""
    from nl_shell.gate import live, rules
    from nl_shell.gate.rules import Verdict

    monkeypatch.setattr(rules, "classify", lambda invocation: Verdict("read-only"))
    monkeypatch.setattr(live, "resolve", lambda invocation, verdict: verdict)
    assert classify("ls -la").cls == "read-only"
    assert classify("ls -la").reasons == ()
    outside = classify("ls -la ~")
    assert outside.cls == "mutating" and has_reason(outside, OUT)
    outside = classify("'ls' -la; ls $x")
    assert outside.cls == "mutating" and has_reason(outside, OUT)
    # advice still raises an accepted command
    assert classify("rm -rf x").cls == "destructive"
    assert classify("sudo ls").cls == "privileged"


def test_live_stub(monkeypatch):
    from nl_shell.gate import live, rules
    from nl_shell.gate.rules import Verdict

    monkeypatch.setattr(rules, "classify", lambda invocation: Verdict("read-only"))
    result = classify("ls")
    assert result.cls == "mutating"
    assert has_reason(result, live.NOT_IMPLEMENTED)


def test_model_label(monkeypatch):
    from nl_shell.gate import live, rules
    from nl_shell.gate.rules import Verdict

    monkeypatch.setattr(rules, "classify", lambda invocation: Verdict("read-only"))
    monkeypatch.setattr(live, "resolve", lambda invocation, verdict: verdict)
    assert classify("ls").cls == "read-only"
    assert classify("ls", label="read-only").cls == "read-only"
    assert classify("ls", label="mutating").cls == "mutating"
    assert classify("ls", label="privileged").cls == "privileged"
    assert classify("rm -rf x", label="read-only").cls == "destructive"


def test_advice_failure_is_not_the_commands_problem(monkeypatch):
    from nl_shell.gate import parse

    def boom(source):
        raise RuntimeError("walker bug")

    monkeypatch.setattr(parse.Walker, "visit", lambda *a, **k: boom(None))
    result = classify("rm -rf x")
    assert result.cls == "mutating" and not result.rejected
    assert has_reason(result, NA)
    # the scan still rejects, whatever the walk does
    assert classify("echo hi >&3").rejected


def test_scan_failure_rejects(monkeypatch):
    from nl_shell.gate import scan

    monkeypatch.setattr(scan, "_scan", lambda *a: 1 / 0)
    result = classify("ls")
    assert result.rejected and result.cls == "privileged"


def test_internal_error_fails_closed(monkeypatch):
    from nl_shell.gate import grammar

    monkeypatch.setattr(grammar, "recognise", lambda source: 1 / 0)
    result = classify("ls")
    assert result.rejected and result.cls == "privileged"
    assert has_reason(result, "could not class")


def test_bad_arguments_fail_closed():
    assert gate().classify("ls", origin="nope").rejected
    assert gate().classify("ls", label="nope").rejected
    assert gate().classify(None).rejected  # type: ignore[arg-type]


def test_reasons_never_carry_raw_input():
    for command in (
        f"ls ‮txt.exe{ESC}[2J",
        f"f{ESC}() {{ :; }}",
        "echo x >\x01out",
        "PATH​=x",
        "sudo \x07-S true",
    ):
        result = classify(command)
        assert_clean(result.reasons)
    result = classify(f"f{ESC}() {{ :; }}")
    assert has_reason(result, "\\x1b")


def test_result_has_session_log_shape():
    result = classify("ls -la")
    log = result.as_log()
    assert set(log) == {"class", "reasons", "rejected"}
    assert log["class"] in CLASSES
    assert isinstance(log["reasons"], list) and all(isinstance(r, str) for r in log["reasons"])
    assert isinstance(log["rejected"], bool)
    assert "redirection" not in " ".join(log["reasons"])
    assert classify("ls -la").reasons == classify("ls -la").reasons  # deterministic


# --- the vectors file ----------------------------------------------------------------


def test_vectors_file_loads():
    from nl_shell.gate import vectors

    rows = vectors.load(VECTORS)
    assert rows
    assert all(v.origin in ("proposal", "typed") for v in rows)
    assert all((v.cls is None) is v.rejected for v in rows)
    rows_with_numbers = [v.row for v in rows if v.row is not None]
    assert len(rows_with_numbers) == len(set(rows_with_numbers)), "duplicate row numbers"


@pytest.mark.parametrize(
    "body,needle",
    [
        ('[[vector]]\norigin = "typed"\nrejected = false\nclass = "mutating"', "command"),
        ('[[vector]]\ncommand = "ls"\nrejected = false\nclass = "mutating"', "origin"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nclass = "mutating"', "rejected"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = false', "class"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = true\nclass = "mutating"', "absent"),
        ('[[vector]]\ncommand = "ls"\norigin = "x"\nrejected = true', "origin"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = true\nextra = 1', "unknown key"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = true\nrow = 0', "row"),
        ('[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = true\nreason = ""', "reason"),
        ('other = 1\n[[vector]]\ncommand = "ls"\norigin = "typed"\nrejected = true', "top-level"),
    ],
)
def test_vectors_loader_rejects(tmp_path, body, needle):
    from nl_shell.gate import vectors

    path = tmp_path / "gate-vectors.toml"
    path.write_text(body)
    with pytest.raises(vectors.VectorError) as info:
        vectors.load(path)
    assert needle in str(info.value)


def test_vectors_loader_rejects_invalid_toml(tmp_path):
    from nl_shell.gate import vectors

    path = tmp_path / "gate-vectors.toml"
    path.write_text("[[vector]\n")
    with pytest.raises(vectors.VectorError) as info:
        vectors.load(path)
    assert "not valid TOML" in str(info.value)
    with pytest.raises(vectors.VectorError) as info:
        vectors.load(tmp_path / "missing.toml")
    assert "missing" in str(info.value)
