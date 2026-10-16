"""The persistent bash session (#140) against pty-protocol v1 (docs/design/specs/pty-protocol).

The tests start a real bash (>= 5.1) on a PTY and need Linux /proc; no VM, no sudo. The
session modules are imported inside each test, so with them missing each test fails on
its own. Every wait on bash has a timeout of a few seconds; every session is closed.
Section letters are the items of #140's success criterion.
"""

from __future__ import annotations

import importlib
import os
import re
import stat
import subprocess
import threading
import time

import pytest

START_TIMEOUT = 5.0  # starting bash and reading its baseline snapshot
RUN_TIMEOUT = 5.0  # one command

SNAPSHOT_TYPES = (b"F", b"L", b"T", b"B", b"X", b"P", b"J", b"H", b"V")


def _session_module():
    return importlib.import_module("nl_shell.session")


def _protocol_module():
    return importlib.import_module("nl_shell.pty_protocol")


class _Records:
    """Every control record a session reported, with the time it was read."""

    def __init__(self) -> None:
        self.items: list[tuple[float, bytes]] = []

    def __call__(self, line: bytes) -> None:
        self.items.append((time.monotonic(), line))

    @property
    def lines(self) -> list[bytes]:
        return [line for _, line in self.items]

    def for_command(self, seq: int) -> list[bytes]:
        """The records from the first `S seq` (or the `D`, if no `S` came) to the next `E`."""
        lines = self.lines
        start = next(
            i for i, line in enumerate(lines)
            if line == b"S %d\n" % seq or line.startswith(b"D %d " % seq)
        )
        end = next(i for i in range(start, len(lines)) if lines[i] == b"E\n")
        return lines[start : end + 1]


@pytest.fixture
def start_session(monkeypatch, tmp_path):
    """Start sessions in tmp_path; close them all and check every record's framing (n).
    ``VTE_VERSION`` is dropped so a VTE system bashrc adds no prompt hook."""
    monkeypatch.delenv("VTE_VERSION", raising=False)
    started = []

    def start(**kwargs):
        session_mod = _session_module()
        records = _Records()
        kwargs.setdefault("cwd", str(tmp_path))
        session = session_mod.Session.start(timeout=START_TIMEOUT, on_record=records, **kwargs)
        started.append((session, records))
        return session, records

    yield start
    for session, records in started:
        session.close()
    for _, records in started:
        for line in records.lines:
            assert line.endswith(b"\n") and b"\r" not in line, line


def _spy_on_pty_writes(monkeypatch) -> list[bytes]:
    """Every write the session module makes to the PTY, from now on."""
    session_mod = _session_module()
    writes: list[bytes] = []
    real_write_all = session_mod._write_all

    def spy(fd, data):
        writes.append(bytes(data))
        real_write_all(fd, data)

    monkeypatch.setattr(session_mod, "_write_all", spy)
    return writes


def _run(session, command, timeout=RUN_TIMEOUT):
    return session.run(command, timeout=timeout)


def _try_run(session, command, timeout=RUN_TIMEOUT):
    """The result, or the SessionDegraded the command raised."""
    session_mod = _session_module()
    try:
        return session.run(command, timeout=timeout)
    except session_mod.SessionDegraded as exc:
        return exc


def _run_default_timeout(session, command, limit=RUN_TIMEOUT):
    """``session.run(command)`` with no timeout, in a thread the test bounds by ``limit``.
    Returns the result or the exception ``run`` raised."""
    outcome: list[object] = []

    def call() -> None:
        try:
            outcome.append(session.run(command))
        except Exception as exc:  # handed back to the test
            outcome.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(limit)
    assert not worker.is_alive(), f"run({command!r}) with no timeout still blocked after {limit} s"
    return outcome[0]


def _pairs(records: list[bytes]) -> tuple[int, int]:
    """Distinct start records (PS0 repeats `S n` per line, §4.1) and done records."""
    starts = {line for line in records if line.startswith(b"S ")}
    dones = sum(1 for line in records if line.startswith(b"D "))
    return len(starts), dones


def _write_bashrc(text: str) -> None:
    home = os.environ["HOME"]  # the suite's temporary HOME (tests/conftest.py)
    with open(os.path.join(home, ".bashrc"), "w", encoding="utf-8") as fh:
        fh.write(text)


def _assert_refused_unsent(session, writes, command, marker=None):
    """``command`` raises SessionDegraded and nothing reaches the PTY."""
    session_mod = _session_module()
    writes.clear()
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, command)
    assert writes == []
    if marker is not None:
        time.sleep(0.2)
        assert not marker.exists()


# ---- (a) state persists between commands --


def test_cd_persists_between_commands(start_session):
    session, _ = start_session()
    _run(session, "cd /tmp")
    assert _run(session, "pwd").text == "/tmp\n"


def test_export_persists_between_commands(start_session):
    session, _ = start_session()
    _run(session, "export FOO=bar")
    assert _run(session, 'printf %s "$FOO"').text == "bar"


# ---- (b) spec §Conformance vectors 1-8, 10 and 12 --


def test_vector1_true_gives_start_done_and_snapshot(start_session):
    session, records = start_session()
    result = _run(session, "true")
    lines = records.for_command(1)
    assert lines[0] == b"S 1\n"
    assert lines[1] == b"D 1 0 0\n"
    assert lines[-1] == b"E\n"
    assert sorted(line[:1] for line in lines[2:-1]) == sorted(SNAPSHOT_TYPES)
    assert (result.seq, result.ran, result.status, result.pipestatus) == (1, True, 0, (0,))
    assert result.snapshot.seq == 1
    assert not session.degraded


@pytest.mark.parametrize(
    "command, status, pipestatus",
    [("false | true", 0, (1, 0)), ("(exit 3)", 3, (3,))],
    ids=["vector2-pipestatus", "vector3-exit-status"],
)
def test_vectors2_3_status(start_session, command, status, pipestatus):
    session, _ = start_session()
    result = _run(session, command)
    assert (result.status, result.pipestatus) == (status, pipestatus)


@pytest.mark.parametrize(
    "command, printed",
    [
        (r"printf 'D 99 0 0\n'", b"D 99 0 0"),
        (r"printf '\x1b]133;D;0\x07'", b"\x1b]133;D;0\x07"),
    ],
    ids=["vector4-fake-done-record", "vector5-osc133"],
)
def test_vectors4_5_printed_records_have_no_effect(start_session, command, printed):
    session, records = start_session()
    result = _run(session, command)
    assert printed in result.output  # it went to the PTY
    dones = [line for line in records.lines if line.startswith(b"D ")]
    assert dones[1:] == [b"D 1 0 0\n"]  # after the baseline's D 0: only the real one
    assert _pairs(records.for_command(1)) == (1, 1)
    assert result.status == 0
    assert not session.degraded


def test_vector6_no_history_expansion(start_session):
    session, _ = start_session()
    assert _run(session, "echo 'done!!'").text == "done!!\n"


def test_vector7_alias_and_function_reach_the_snapshot(start_session):
    session, _ = start_session()
    assert "g" not in session.baseline.functions
    assert "ll2" not in session.baseline.aliases
    result = _run(session, "alias ll2='ls -la'; g(){ :; }")
    assert "g" in result.snapshot.functions
    assert "alias ll2='ls -la'" in result.snapshot.aliases.split("\n")


def test_vector8_replaced_prompt_command_degrades(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    began = time.monotonic()
    with pytest.raises(session_mod.SessionDegraded) as caught:
        _run(session, "PROMPT_COMMAND=:")
    assert time.monotonic() - began < 5.0
    # Found at that command's own snapshot (H changed), not by a later rule.
    assert caught.value.result is not None and caught.value.result.seq == 1
    assert session.degraded
    assert "H differs" in session.degraded_reason
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "true")  # sticky until restart


def test_vector10_interrupt_ends_the_command(start_session):
    session, records = start_session()
    sent_at: list[float] = []
    sent: list[bool] = []

    def press_ctrl_c() -> None:
        deadline = time.monotonic() + RUN_TIMEOUT
        while b"S 1\n" not in records.lines:
            if time.monotonic() > deadline:
                return
            time.sleep(0.01)
        time.sleep(0.3)
        sent_at.append(time.monotonic())
        sent.append(session.interrupt())

    typist = threading.Thread(target=press_ctrl_c, daemon=True)
    typist.start()
    result = _run(session, "sleep 5", timeout=8.0)
    typist.join(timeout=2)
    assert sent_at, "the interrupt was never sent"
    assert sent == [True]
    assert result.status == 130
    assert result.pipestatus == (130,)
    done_at = next(t for t, line in records.items if line.startswith(b"D 1 "))
    assert done_at - sent_at[0] < 3.0
    assert not session.degraded
    assert _run(session, "echo after").text == "after\n"


def test_interrupt_at_a_prompt_writes_nothing(start_session, monkeypatch):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, records = start_session()
    assert session.interrupt() is False  # idle, before any command
    _run(session, "true")
    pty_writes.clear()
    assert session.interrupt() is False  # idle, after a command finished
    assert pty_writes == []
    before = len(records.items)
    time.sleep(1.0)  # a Ctrl-C at the prompt would have made bash send a stray D
    assert len(records.items) == before
    assert not session.degraded
    result = _run(session, "echo still fine")
    assert (result.seq, result.text) == (2, "still fine\n")


@pytest.mark.parametrize(
    "bad",
    ["echo a\x1b[201~", "echo \x1b", "echo a\rtouch {d}/c", "echo \x07", "echo \x00",
     "echo \x01", "echo \x1f", "echo \x03"],
    ids=["paste-end", "esc", "cr", "bel", "nul", "soh", "us", "etx"],
)
def test_vector12_forbidden_bytes_are_refused_unsent(start_session, monkeypatch, tmp_path, bad):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session_mod = _session_module()
    work = tmp_path / "work"
    work.mkdir()
    session, records = start_session()
    before = len(records.items)
    command = f"touch {work}/a; {bad.replace('{d}', str(work))}; touch {work}/b"
    pty_writes.clear()
    with pytest.raises(session_mod.CommandRefused):
        _run(session, command)
    assert pty_writes == []  # nothing reached the PTY
    time.sleep(0.3)
    assert len(records.items) == before
    assert list(work.iterdir()) == []
    result = _run(session, "echo ok")
    assert result.seq == 1  # nothing ran in between
    assert result.text == "ok\n"


def test_tab_and_newline_are_allowed(start_session):
    session, _ = start_session()
    assert _run(session, "printf '%s\\n' 'a\tb'\necho c").text == "a\tb\nc\n"


def test_paste_is_exactly_the_confirmed_bytes(start_session, monkeypatch):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    pty_writes.clear()
    _run(session, "echo 'x  y'")
    assert pty_writes == [b"\x1b[200~echo 'x  y'\x1b[201~\r"]


# ---- (c) vector 9 typed raw: the control channel closed --


def test_vector9_closing_fd3_degrades(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "exec 3>&-")
    assert session.degraded
    assert session.degraded_reason == "the control channel closed"


# ---- the hooks survive set -u / set -e --


def test_nounset_without_path_keeps_the_snapshot_whole(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    result = _run_default_timeout(session, "set -u; unset PATH")
    assert isinstance(result, session_mod.CommandResult), result
    assert result.snapshot.path == ""
    assert not session.degraded
    after = _run_default_timeout(session, "echo still here")
    assert isinstance(after, session_mod.CommandResult), after
    assert after.text == "still here\n"


def test_errexit_does_not_break_the_hooks(start_session):
    session, _ = start_session()
    _run(session, "set -e")
    for command in ("true", "", "echo ok"):
        _run(session, command)
    assert _run(session, "echo still ok").text == "still ok\n"
    assert not session.degraded


# ---- (d) one start/done pair per command; stopped jobs --


@pytest.mark.parametrize(
    "command, output",
    [
        ("true", ""),
        ("echo one\necho two", "one\ntwo\n"),
        ("cat <<'EOF'\nhere doc\nEOF", "here doc\n"),
        ("f() {\n  echo in f\n}\nf", "in f\n"),
    ],
    ids=["no-output", "multi-line", "here-doc", "multi-line-function"],
)
def test_one_start_done_pair_per_command(start_session, command, output):
    session, records = start_session()
    result = _run(session, command)
    lines = records.for_command(1)
    assert _pairs(lines) == (1, 1)
    assert all(line == b"S 1\n" for line in lines if line.startswith(b"S "))
    assert result.ran
    assert result.text == output
    assert _run(session, "true").seq == 2


def test_background_job_gives_one_pair_and_is_not_a_stopped_job(start_session):
    session, records = start_session()
    result = _run(session, "sleep 1 &")
    assert _pairs(records.for_command(1)) == (1, 1)
    assert result.status == 0
    assert not session.degraded
    result = _run(session, "sleep 3 &")
    assert result.snapshot.stopped_jobs == 0  # running, not stopped
    assert _run(session, "echo still here").text.endswith("still here\n")
    assert not session.degraded


def test_stopped_job_is_counted(start_session):
    session, _ = start_session()
    _run(session, "sleep 30 & kill -STOP $!")
    assert _run(session, "true").snapshot.stopped_jobs == 1
    assert _run(session, "kill -KILL %1; wait %1 2>/dev/null; true").status == 0
    assert _run(session, "true").snapshot.stopped_jobs == 0


# ---- (e) decision 5: nothing ran; the prompt rule fails closed --


@pytest.mark.parametrize(
    "command, status",
    [("", 3), ("# x", 3), ("fi", 2)],
    ids=["empty-line", "comment-only", "syntax-error"],
)
def test_input_that_runs_nothing_is_reported_not_run(start_session, command, status):
    session, records = start_session()
    _run(session, "(exit 3)")
    result = _run(session, command)
    assert not result.ran
    assert result.seq == 2
    assert result.status == status  # the D record's $?
    assert _pairs(records.for_command(2)) == (0, 1)
    assert not session.degraded
    after = _run(session, "true")
    assert (after.seq, after.ran, after.status) == (3, True, 0)


def test_unclosed_quote_degrades(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    began = time.monotonic()
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "echo 'abc")
    assert time.monotonic() - began < 5.0
    assert session.degraded_reason == "no done record after the shell returned to a prompt"


def test_unreadable_proc_syscall_at_start_degrades(start_session, monkeypatch, tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()  # no <pid>/syscall in it
    session, _ = start_session(proc_root=str(fake_proc))
    path = os.path.join(str(fake_proc), str(session.pid), "syscall")
    assert session.degraded
    assert path in session.degraded_reason
    began = time.monotonic()
    _assert_refused_unsent(session, pty_writes, "echo 'abc")
    assert time.monotonic() - began < 5.0
    assert path in session.degraded_reason


def test_unreadable_proc_syscall_during_a_run_degrades(start_session, tmp_path):
    session_mod = _session_module()
    session, records = start_session()
    assert not session.degraded
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()

    def lose_proc() -> None:
        deadline = time.monotonic() + RUN_TIMEOUT
        while b"S 1\n" not in records.lines and time.monotonic() < deadline:
            time.sleep(0.01)
        session.proc_root = str(fake_proc)

    switcher = threading.Thread(target=lose_proc, daemon=True)
    switcher.start()
    began = time.monotonic()
    # `read x` is the case that needs the prompt rule: with no D, only /proc can tell.
    outcome = _run_default_timeout(session, "sleep 0.3; read x")
    switcher.join(timeout=2)
    assert time.monotonic() - began < 5.0
    assert isinstance(outcome, session_mod.SessionDegraded), outcome
    assert outcome.result is None  # never "ran" or "not run" with a healthy session
    path = os.path.join(str(fake_proc), str(session.pid), "syscall")
    assert path in session.degraded_reason


# ---- (f) snapshot fields, the baseline, H and V --


def test_snapshot_carries_v1_fields_and_baseline(start_session):
    session, records = start_session()
    baseline = session.baseline
    types = {line[:1] for line in records.for_command(0)}
    assert set(SNAPSHOT_TYPES) | {b"D", b"E"} == types
    assert baseline.seq == 0
    assert baseline.extdebug == "shopt -u extdebug"
    assert baseline.path == os.environ["PATH"]
    assert baseline.stopped_jobs == 0
    assert baseline.disabled_builtins == ""
    for name in ("PROMPT_COMMAND", "PS0", "__nls_done", "__nls_user_pc", "promptvars"):
        assert name in baseline.hooks
    assert 'declare -- __nls_seq="0"' in baseline.hooks.split("\n")
    assert "LD_PRELOAD unset" in baseline.variables.split("\n")
    result = _run(session, "true")
    assert session.baseline is baseline
    assert 'declare -- __nls_seq="1"' in result.snapshot.hooks.split("\n")
    assert not result.variables_changed


def test_loader_variable_change_is_reported(start_session):
    session, _ = start_session()
    result = _run(session, "export LD_PRELOAD=/x.so")
    assert result.variables_changed
    assert result.snapshot.variables != session.baseline.variables
    assert 'declare -x LD_PRELOAD="/x.so"' in result.snapshot.variables.split("\n")
    assert not session.degraded


def test_blank_ps0_is_not_hidden_as_not_run(start_session, monkeypatch, tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session_mod = _session_module()
    session, _ = start_session()
    with pytest.raises(session_mod.SessionDegraded) as caught:
        _run(session, "PS0=")
    assert caught.value.result is not None and caught.value.result.seq == 1
    assert "H differs" in session.degraded_reason
    _assert_refused_unsent(session, pty_writes, "true")  # never "not run" while healthy
    marker = tmp_path / "ran"
    _assert_refused_unsent(session, pty_writes, f"touch {marker}; true", marker)


_TABLE_A_ROW_1 = {
    "prompt-command-array": "PROMPT_COMMAND+=(':')",
    "prompt-command-appended": "PROMPT_COMMAND=\"$PROMPT_COMMAND; :\"",
    "ps0": "PS0='x'",
    "promptvars-off": "shopt -u promptvars",
    "hook-function-redefined": "__nls_ret() { builtin return \"$1\"; }",
    "main-hook-redefined": "__f=$(declare -f __nls_done); eval \"${__f/\\{/\\{ :;}\"",
    "saved-user-hook": "__nls_user_pc+=(':')",
    "state-variable-integer": "declare -i __nls_n",
    "ps1-readonly": "readonly PS1",
    # nl-shell can't overwrite it; its `$( … )` may still run once at the next prompt.
    "ps1-readonly-command": "declare -r PS1='$(touch PS1_RAN)\\$ '",
    "term-dumb": "export TERM=dumb",  # readline would turn bracketed paste off
}


@pytest.mark.parametrize("change", sorted(_TABLE_A_ROW_1))
def test_table_a_row_1_changes_degrade_at_their_own_snapshot(start_session, monkeypatch,
                                                             change):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session_mod = _session_module()
    session, _ = start_session()
    with pytest.raises(session_mod.SessionDegraded) as caught:
        _run(session, _TABLE_A_ROW_1[change])
    # Found in the snapshot that followed this command's D, not by a later rule.
    assert caught.value.result is not None, session.degraded_reason
    assert caught.value.result.seq == 1
    assert "H differs" in session.degraded_reason
    _assert_refused_unsent(session, pty_writes, "echo hello")


@pytest.mark.parametrize("value", ["42", "(42)"], ids=["scalar", "array"])
def test_hook_state_value_changes_degrade_at_their_own_snapshot(start_session, monkeypatch,
                                                                value):
    # I2-b: the value of every hook state variable, including the scratch ones the hooks
    # overwrite (their value before the hooks ran is kept in `__nls_pre`).
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session_mod = _session_module()
    for name in session_mod.HOOK_STATE_VARS:
        session, _ = start_session()
        with pytest.raises(session_mod.SessionDegraded) as caught:
            _run(session, f"{name}={value}")
        if name == "__nls_seq":  # caught one record earlier: that command's own D
            assert "out of order" in session.degraded_reason, session.degraded_reason
            _assert_refused_unsent(session, pty_writes, "echo hello")
            session.close()
            continue
        assert caught.value.result is not None, (name, session.degraded_reason)
        assert caught.value.result.seq == 1, name
        assert "H differs" in session.degraded_reason, (name, session.degraded_reason)
        _assert_refused_unsent(session, pty_writes, "echo hello")
        session.close()


@pytest.mark.parametrize(
    "change",
    ["readonly FOO=1", "trap 'echo hi' EXIT", "PS1='x$ '", "PS2='more> '"],
    ids=["readonly-other", "exit-trap", "ps1", "ps2"],
)
def test_ordinary_changes_stay_healthy(start_session, change):
    session, _ = start_session()
    result = _run(session, change)
    assert result.ran and result.status == 0
    assert not session.degraded
    after = _run(session, "echo ok")
    assert after.text == "ok\n"
    assert not session.degraded
    if "trap" in change:
        assert "trap -- 'echo hi' EXIT" in after.snapshot.traps


# ---- (g) I2: a command that ran is never reported as "nothing ran" --


def _rcfile_variables(rcfile: str) -> set[str]:
    """Every shell variable the rcfile reads or assigns, by bash syntax."""
    patterns = (
        r"\$\{[#!]?([A-Za-z_]\w*)",  # ${name...}, ${#name}, ${!name}
        r"\$([A-Za-z_]\w*)",  # $name
        r"\$\(\(\s*([A-Za-z_]\w*)",  # $((name ...
        r"\(\(\s*\+\+([A-Za-z_]\w*)",  # (( ++name
        r"(?:^|[\s;(])([A-Za-z_]\w*)(?:\[[^\]]*\])?\+?=",  # name=, name[i]=, name+=
        r"\bfor\s+([A-Za-z_]\w*)\s+in\b",
        r"\bprintf\s+-v\s+([A-Za-z_]\w*)",
        r"\bread\s+(?:-\w+\s+(?:\d+\s+)?)*([A-Za-z_]\w*)",
        r"\[\[\s+-v\s+\$?([A-Za-z_]\w*)",
    )
    names: set[str] = set()
    for pattern in patterns:
        names.update(re.findall(pattern, rcfile, flags=re.MULTILINE))
    return names


def test_hook_state_vars_cover_the_rcfile():
    session_mod = _session_module()
    rcfile = session_mod.RCFILE
    listed = set(session_mod.HOOK_STATE_VARS)
    assert all(name.startswith("__nls_") for name in listed)
    found = set(re.findall(r"__nls_\w+", rcfile))
    assert found <= listed, found - listed
    assert listed <= found, listed - found
    # Every other variable is bash's own; no `local` (a readonly global would break it).
    bash_owned = {"BASHOPTS", "PIPESTATUS", "PATH", "PROMPT_COMMAND", "PS0", "PS1", "PS2",
                  "HISTFILE"}
    variables = _rcfile_variables(rcfile)
    assert {"__nls_seq", "__nls_st", "__nls_v", "__nls_c"} <= variables  # the parser works
    stray = {name for name in variables if not name.startswith("__nls_")} - bash_owned
    assert stray == set(), stray
    assert not re.search(r"\blocal\b", rcfile)


def _ends_degraded_or_ran(session, marker):
    """The command after ``true`` is degraded or reported as ran, never "ran False"."""
    session_mod = _session_module()
    _try_run(session, "true")
    third = _try_run(session, f"touch {marker}; echo printed; (exit 7)")
    if session.degraded:
        if not isinstance(third, session_mod.SessionDegraded) or third.result is None:
            assert not marker.exists(), marker.name
        return
    assert (third.ran, third.status, third.text) == (True, 7, "printed\n"), marker.name
    assert marker.exists()


# Each sets the variable to what a start guard would need to suppress a later S.
_I2_ATTACKS = {
    "ps1-subscript": "PS1='${{__pwn[{name}=__nls_seq]-}}\\$ '",
    "readonly": "readonly {name}=$((__nls_seq + 2))",
    "debug-trap": "trap '{name}=$__nls_seq' DEBUG",
}


@pytest.mark.parametrize("attack", sorted(_I2_ATTACKS))
def test_hook_state_attacks_never_hide_a_command(start_session, tmp_path, attack):
    for name in _session_module().HOOK_STATE_VARS:
        session, _ = start_session()
        _try_run(session, _I2_ATTACKS[attack].format(name=name))
        _ends_degraded_or_ran(session, tmp_path / f"{attack}-{name}")
        session.close()


@pytest.mark.parametrize(
    "aliases",
    # `builtin` ending in a space lets `printf` expand too: an alias could drop S.
    ['__p(){ [[ $1 == S* ]] || builtin printf "$@"; }; alias builtin=" " printf=__p',
     "alias for=': ' if=': ' '{'=': '"],
    ids=["builtin-printf", "reserved-words"],
)
def test_aliases_cannot_reach_inside_the_hooks(start_session, tmp_path, aliases):
    session, _ = start_session()
    _try_run(session, aliases)
    _ends_degraded_or_ran(session, tmp_path / "mark")
    session, _ = start_session()
    _run(session, "alias ls='touch PWNED' builtin=' ' alias=':'")
    _try_run(session, "ls")
    assert "alias ls='touch PWNED'" in session.snapshot.aliases
    session, _ = start_session()  # the user's aliases still work after a snapshot
    assert _run(session, "alias zz='echo zz'; shopt -u expand_aliases").ran
    assert _run(session, "shopt -s expand_aliases").ran and _run(session, "zz").text == "zz\n"


# ---- (k) the prompt rule (decision 7) --


@pytest.mark.parametrize(
    "command, output",
    [
        ("x=$(sleep 2); echo hi", "hi\n"),
        ('cd "$(sleep 1.5; echo /tmp)"; pwd', "/tmp\n"),
        ("y=`sleep 1.5`; echo \"[$y]\"", "[]\n"),
    ],
    ids=["dollar-paren", "cd-substitution", "backticks"],
)
def test_slow_command_substitution_stays_healthy(start_session, command, output):
    session, _ = start_session()
    result = _run(session, command)
    assert result.text == output
    assert not session.degraded


@pytest.mark.parametrize(
    "command, output",
    [
        ('read x < <(sleep 1.5; echo hi); echo "$x"', "hi\n"),
        ("while read -r l; do echo got $l; done < <(sleep 2; echo hi)", "got hi\n"),
        ("mapfile -t a < <(sleep 1.5; printf 'a\\nb\\n'); echo \"${a[*]}\"", "a b\n"),
    ],
    ids=["read-process-substitution", "while-read-loop", "mapfile"],
)
def test_slow_read_from_redirected_stdin_stays_healthy(start_session, command, output):
    # bash blocks in read(0) here, but fd 0 is a pipe, not the terminal: not a prompt.
    session, _ = start_session()
    result = _run(session, command)
    assert result.ran and result.status == 0
    assert result.text == output
    assert not session.degraded


def test_unreadable_proc_stdin_fails_closed(start_session, monkeypatch, tmp_path):
    session_mod = _session_module()
    session, _ = start_session()
    assert not session.degraded
    pid_dir = tmp_path / "proc" / str(session.pid)
    pid_dir.mkdir(parents=True)
    # syscall is readable, but there is no fd/0 to tell the terminal from a pipe.
    os.symlink(f"/proc/{session.pid}/syscall", pid_dir / "syscall")
    session.proc_root = str(tmp_path / "proc")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    began = time.monotonic()
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "read x")
    assert time.monotonic() - began < 5.0
    assert pty_writes == []  # bash at its prompt: the check fails before the paste
    assert os.path.join(str(pid_dir), "fd", "0") in session.degraded_reason


@pytest.mark.parametrize("command", ["read x", "read x </dev/tty"])
def test_read_with_no_input_degrades(start_session, command):
    session_mod = _session_module()
    session, _ = start_session()
    began = time.monotonic()
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, command)
    assert time.monotonic() - began < 5.0
    assert session.degraded_reason == "no done record after the shell returned to a prompt"


# ---- (l) decision 4: exact, reversible fields; no external programs --


@pytest.mark.parametrize(
    "command, expression, field, needles",
    [
        ("PATH=\"$PATH\"$':/a\\x1eb:/c\\nd:/e\\\\nf\\rg'", "$PATH", "path",
         ("\x1e", "\n", "\\n", "\r")),
        ("alias odd_nls=$'a\\\\b\\x1e\\nc \\'q\\''", "$(alias -p)", "aliases",
         ("odd_nls", "\x1e", "\n")),
        ("trap $'echo \"x\\\\y\"\\x1e\\n: z' USR1", "$(trap -p)", "traps",
         ("USR1", "\x1e", "\\")),
    ],
    ids=["path", "alias", "trap"],
)
def test_text_fields_round_trip_exactly(start_session, tmp_path, command, expression, field,
                                        needles):
    session, _ = start_session()
    result = _run(session, command)
    out = tmp_path / "live"
    _run(session, f'printf %s "{expression}" > {out}')
    live = out.read_bytes().decode("utf-8")
    assert all(needle in live for needle in needles)
    assert getattr(result.snapshot, field) == live
    assert not session.degraded


@pytest.mark.parametrize(
    "setup",
    [
        "hash -p {fake}/tr tr",
        "hash -p {fake}/cksum cksum",
        "hash -p {fake}/wc wc",
        "BASH_CMDS[tr]={fake}/tr",
        "PATH={fake}:$PATH",
    ],
    ids=["hash-tr", "hash-cksum", "hash-wc", "bash-cmds-tr", "path-first"],
)
def test_fake_programs_cannot_change_the_snapshot(start_session, tmp_path, setup):
    fake = tmp_path / "fake"
    fake.mkdir()
    marker = tmp_path / "fake-ran"
    for name in ("tr", "cksum", "wc"):
        script = fake / name
        script.write_text(f"#!/bin/sh\ntouch {marker}\necho 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    session, _ = start_session()
    command = setup.format(fake=fake) + "; alias evil_nls='rm -rf'; evil_fn(){ :; }"
    result = _run(session, command)
    snap = _run(session, "true").snapshot
    protocol = _protocol_module()
    for s in (result.snapshot, snap):
        assert "alias evil_nls='rm -rf'" in s.aliases.split("\n")
        assert "evil_fn" in s.functions
        assert s.hooks == protocol.expected_hooks(session.baseline, s.seq)
    assert not marker.exists()
    assert not session.degraded


# ---- (m, first half) the user's ~/.bashrc --


_NLS_HOOK = '\\__nls_done; \\__nls_snap "$(\\builtin trap -p)"'


def _prompt_settings(hooks: str) -> tuple[str, str, list[str]]:
    """``PS1``, ``PS2`` and the ``PROMPT_COMMAND`` elements, as the ``declare -p`` lines
    in an ``H`` text set them (evaluated by a clean bash)."""
    lines = [line for line in hooks.split("\n")
             if re.match(r"declare -\S+ (PROMPT_COMMAND|PS1|PS2)(=|$)", line)]
    assert len(lines) == 3, hooks
    shown = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c",
         'eval "$1"; printf "%s\\0" "$PS1" "$PS2" "${PROMPT_COMMAND[@]}"', "_",
         "\n".join(lines)],
        capture_output=True, check=True, timeout=RUN_TIMEOUT,
    ).stdout.decode("utf-8").split("\0")[:-1]
    return shown[0], shown[1], shown[2:]


def test_bashrc_alias_and_prompt_command_run_before_the_snapshot(start_session):
    # The user's PROMPT_COMMAND is a two-element array; the second sees what the first set.
    _write_bashrc(
        "alias hello_nls='echo hi'\n"
        "PROMPT_COMMAND=('s=$?; alias pc_saw_status=\"$s\"'"
        " 'echo \"$__nls_seq $s\" >> ~/pc.log')\n"
    )
    session, _ = start_session()
    baseline_aliases = session.baseline.aliases.split("\n")
    assert "alias hello_nls='echo hi'" in baseline_aliases
    # The user's hook ran at the first prompt before the baseline was taken (decision 6).
    assert "alias pc_saw_status='0'" in baseline_aliases
    snapshots = [session.baseline]
    result = _run(session, "(exit 4)")
    snapshots.append(result.snapshot)
    # It ran again before this command's snapshot, and saw the command's real $?.
    assert "alias pc_saw_status='4'" in result.snapshot.aliases.split("\n")
    snapshots.append(_run(session, "unalias pc_saw_status").snapshot)
    after = _run(session, "true")
    snapshots.append(after.snapshot)
    assert "alias pc_saw_status='0'" in after.snapshot.aliases.split("\n")
    with open(os.path.join(os.environ["HOME"], "pc.log"), encoding="utf-8") as fh:
        seen = fh.read().split("\n")
    assert seen[:4] == ["0 0", "1 4", "2 0", "3 0"]
    # I3: in every H, nl-shell's hook is the last PROMPT_COMMAND element (no user element
    # is left in PROMPT_COMMAND to run after it) and PS1/PS2 are nl-shell's fixed values.
    for snap in snapshots:
        ps1, ps2, elements = _prompt_settings(snap.hooks)
        assert (ps1, ps2) == ("\\$ ", "> "), snap.seq
        assert elements[-1] == _NLS_HOOK, snap.seq
        assert all(element == "" for element in elements[:-1]), elements
    assert not session.degraded


# ---- (n) no history; control-channel wiring and framing --


def test_histfile_is_unset(start_session):
    session, _ = start_session()
    assert _run(session, 'printf %s "${HISTFILE-unset}"').text == "unset"
    session.close()
    assert not os.path.exists(os.path.join(os.environ["HOME"], ".bash_history"))


def test_fd3_is_a_pipe(start_session):
    session, _ = start_session()
    result = _run(session, "[ -p /dev/fd/3 ]")
    assert (result.ran, result.status) == (True, 0)


def test_record_with_carriage_return_is_malformed():
    protocol = _protocol_module()
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_record(b"S 1\r")
    tracker = protocol.ProtocolTracker()
    tracker.feed(b"D 0 0 0\r\n")
    assert tracker.degraded
    assert "carriage return" in tracker.degraded_reason


def _baseline_records(hooks: bytes = b'declare -- __nls_seq="0"\\n') -> bytes:
    return b"D 0 0 0\nF \nL \nT \nB \nX \nP \nJ 0\nH " + hooks + b"\nV \nE\n"


def test_unknown_record_types_are_ignored():
    protocol = _protocol_module()
    tracker = protocol.ProtocolTracker()
    tracker.feed(b"D 0 0 0\nQ something new\n")
    assert not tracker.degraded


def test_repeated_start_record_is_one_start_but_another_number_degrades():
    protocol = _protocol_module()
    tracker = protocol.ProtocolTracker()
    tracker.feed(_baseline_records())
    assert not tracker.degraded
    tracker.command_sent()
    tracker.feed(b"S 1\nS 1\nS 1\n")
    assert not tracker.degraded
    tracker.feed(b"S 2\n")
    assert tracker.degraded


def test_done_without_start_is_nothing_ran_and_h_tracks_the_counter():
    protocol = _protocol_module()
    tracker = protocol.ProtocolTracker()
    tracker.feed(_baseline_records())
    tracker.command_sent()
    second = _baseline_records(b'declare -- __nls_seq="1"\\n').replace(b"D 0", b"D 1")
    events = tracker.feed(second)
    assert [kind for kind, _ in events] == ["D", "E"]
    assert not tracker.started and not tracker.degraded
    tracker.command_sent()
    # The counter's line must carry the D record's number; anything else in H is a change.
    wrong = _baseline_records(b'declare -- __nls_seq="7"\\n').replace(b"D 0", b"D 2")
    tracker.feed(b"S 2\n" + wrong)
    assert tracker.degraded
    assert "H differs" in tracker.degraded_reason


def test_spec_reference_rcfile_is_the_one_in_use():
    session_mod = _session_module()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = os.path.join(root, "docs", "design", "specs", "pty-protocol", "v1.md")
    with open(spec, encoding="utf-8") as fh:
        text = fh.read()
    section = text[text.index("3. Reference rcfile") :]
    reference = section[section.index("```bash\n") + 8 : section.index("```\n")]
    assert session_mod.RCFILE.split("\n", 1)[1] == reference


def test_field_encoding_is_reversible():
    protocol = _protocol_module()
    assert protocol.decode_field("a\\\\n\\nb\\r") == "a\\n\nb\r"
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_field("bad\\x")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_field("trailing\\")


# ---- (o) restart --


def test_restart_after_degraded_gives_a_fresh_session(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    old_baseline = session.baseline
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "PROMPT_COMMAND=:")
    session.restart(timeout=START_TIMEOUT)
    assert not session.degraded
    assert session.degraded_reason is None
    assert session.baseline is not old_baseline
    assert session.baseline.seq == 0
    result = _run(session, "echo back")
    assert (result.seq, result.text) == (1, "back\n")


# ---- (q) output boundary: what the user's prompt hook prints is not the command's --


def test_user_prompt_hook_output_is_not_the_commands(start_session):
    _write_bashrc("PROMPT_COMMAND=\"printf 'HOOK-OUTPUT\\\\n'; echo x >> ~/hook.log\"\n")
    session, _ = start_session()
    assert session.baseline is not None
    for _ in range(20):
        assert _run(session, "echo hello").output == b"hello\r\n"
        assert _run(session, "printf abc").output == b"abc"
    assert not session.degraded
    with open(os.path.join(os.environ["HOME"], "hook.log"), encoding="utf-8") as fh:
        assert len(fh.read().split()) == 41  # the hook ran (and printed) at every prompt


def test_job_notice_is_part_of_the_next_commands_output(start_session):
    # Spec §4.1: bash prints a job's notice before its next prompt, between S and D.
    session, _ = start_session()
    _run(session, "sleep 0.2 &")
    time.sleep(0.6)
    result = _run(session, "echo x")
    assert re.search(rb"\[1\]\+ +Done +sleep 0\.2\r\n", result.output), result.output
    assert b"x\r\n" in result.output
    assert not session.degraded


# ---- captured output is bounded --


def test_captured_output_is_bounded_and_keeps_the_tail(start_session):
    session, _ = start_session(max_output=1000)
    result = _run(session, "printf 'x%.0s' {1..5000}; echo; echo END_MARK")
    assert result.output_truncated
    assert len(result.output) <= 1000
    assert result.text.endswith("END_MARK\n")
    assert not _run(session, "echo small").output_truncated
