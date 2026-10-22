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


def test_result_carries_the_snapshot_from_before_the_command(start_session):
    session, _ = start_session()
    _run(session, "f(){ unset -f f; return 1; }")
    result = _run(session, "f")
    assert result.status == 1
    assert "f" in result.before.functions
    assert "f" not in result.snapshot.functions


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
    assert session.degraded_code == "missing-record"
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
    assert session.degraded_code == "missing-record"


def test_no_prompt_before_a_paste_degrades_as_missing_record(start_session, monkeypatch):
    session_mod = _session_module()
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert not session.degraded
    monkeypatch.setattr(session_mod, "_SETTLE", 0.2)
    monkeypatch.setattr(session, "_reading_terminal", lambda: False)  # never at a prompt
    outcome = _try_run(session, "true")
    assert isinstance(outcome, session_mod.SessionDegraded)
    assert outcome.reason == "the shell did not return to its prompt"
    assert outcome.code == session.degraded_code == "missing-record"
    _assert_refused_unsent(session, pty_writes, "echo hello")


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
    assert stray == {"LC_ALL"}, stray
    # LC_ALL is set once, in __nls_enc's subshell, so the shell's own value never changes.
    enc = re.search(r"^__nls_enc\(\) \{\n  \(\n(.*?)\n  \)\n\}$", rcfile, re.M | re.S)
    assert enc is not None and "LC_ALL=C" in enc.group(1)
    assert rcfile.count("LC_ALL") == 1
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
        ("trap $'echo \"x\\\\y\"\\x1e\\n: z' EXIT", "$(trap -p)", "traps",
         ("EXIT", "\x1e", "\\")),
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


def _nls_hook() -> str:
    """nl-shell's PROMPT_COMMAND element, as the rcfile in use sets it."""
    head = "PROMPT_COMMAND=('' '"
    line = next(line for line in _session_module().RCFILE.split("\n") if line.startswith(head))
    assert line.endswith("')"), line
    return line[len(head) : -len("')")]


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
        assert elements[-1] == _nls_hook(), snap.seq
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


# ==== #141: tamper checks (new H rows, the outside checks, reason codes) ====
#
# "Marker" is a file the injected code would create; each test asserts it does NOT exist.
# "Refused" is: run() raises SessionDegraded and the spied PTY write count stays zero.
# Each test asserts the attack's outcome (degraded / refused / no marker) before the reason
# code, so without the fix it fails on the attack, not on a missing code.

# A key binding that makes Enter (which ends every paste, §5.1) type a command of its own.
_BIND_ENTER = r"""bind '"\r": " ; touch MARK\n"'"""
# The same `bind`, for use inside a single-quoted trap or function body.
_BIND_ENTER_INNER = r'''bind "\"\\r\": \" ; touch MARK\\n\""'''


def _assert_degrades_at_its_snapshot(session, command, code):
    """``command`` degrades the session at its own snapshot (or its own E), with reason
    ``code``; returns the SessionDegraded it raised."""
    session_mod = _session_module()
    outcome = _try_run(session, command)
    assert isinstance(outcome, session_mod.SessionDegraded), (command, outcome)
    assert outcome.result is not None, (command, session.degraded_reason)
    assert outcome.result.seq == 1, command
    assert session.degraded_code == code, (command, session.degraded_code,
                                           session.degraded_reason)
    assert outcome.code == code
    return outcome


def _refused_with_code(session, writes, command, code, marker=None):
    """``command`` is refused (no PTY write) and the degrade reason code is ``code``."""
    session_mod = _session_module()
    writes.clear()
    with pytest.raises(session_mod.SessionDegraded) as caught:
        _run(session, command)
    assert writes == []
    if marker is not None:
        time.sleep(0.2)
        assert not marker.exists()
    assert caught.value.code == code, (caught.value.code, caught.value.reason)
    assert session.degraded_code == code


# ---- (h) the rows now in H, and fds 0-2 (outside check) --


_TABLE_A_IN_H = {
    "debug-trap": "trap ':' DEBUG",
    "return-trap": "trap ':' RETURN",
    "usr1-trap": "trap ':' USR1",
    "winch-trap": "trap ':' WINCH",
    "err-trap": "trap ':' ERR",
    "chld-trap": "trap ':' CHLD",  # runs each time a `$( … )` of the hooks ends
    "functrace": "set -o functrace",
    "errtrace": "set -o errtrace",
    "extdebug": "shopt -s extdebug",
    "histexpand": "set -H",
    "xtrace": "set -x",
    "vi-mode": "set -o vi",
    "readline-off": "set +o emacs",
    "bind-enter-macro": _BIND_ENTER,
    "bind-x-enter": r"""bind -x '"\C-m": "touch MARK"'""",
    "bind-paste-prefix": r"""bind '"\e[2": "touch MARK\n"'""",
    "bind-meta-paste-start": r"""bind -m emacs-meta '"[200~": "touch MARK\n"'""",
    "bracketed-paste-off": "bind 'set enable-bracketed-paste off'",
    "convert-meta": "bind 'set convert-meta on'",
}


@pytest.mark.parametrize("change", sorted(_TABLE_A_IN_H))
def test_table_a_rows_degrade_at_their_own_snapshot(start_session, monkeypatch, tmp_path,
                                                    change):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    command = _TABLE_A_IN_H[change]
    if change == "convert-meta" and "set convert-meta on" in session.baseline.hooks.split("\n"):
        command = "bind 'set convert-meta off'"  # the locale already turned it on: flip it
    assert not session.degraded
    _assert_degrades_at_its_snapshot(session, command, "h-changed")
    marker = tmp_path / "MARK"
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert not marker.exists()


@pytest.mark.parametrize(
    "command, code",
    [("exec >/dev/null", "outside-check"), ("exec 2>/dev/null", "outside-check"),
     ("exec </dev/null", None)],
    ids=["stdout", "stderr", "stdin"],
)
def test_redirected_shell_fds_are_refused(start_session, monkeypatch, tmp_path, command,
                                          code):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    if code is not None:
        _assert_degrades_at_its_snapshot(session, command, code)
    else:
        _try_run(session, command)  # bash may read EOF and exit first
    _assert_refused_unsent(session, pty_writes, f"touch {marker}; echo hello", marker)


# ---- (h2) code that runs synchronously after D, before readline --


_H2_ATTACKS = {
    "debug-trap-before-snap":
        "trap 'if [[ $BASH_COMMAND == *__nls_snap* ]]; then trap - DEBUG; "
        + _BIND_ENTER_INNER + "; fi' DEBUG",
    "debug-trap-before-done":
        "trap 'if [[ $BASH_COMMAND == *__nls_done* ]]; then trap - DEBUG; "
        + _BIND_ENTER_INNER + "; fi' DEBUG",
    # Fires inside __nls_snap after H is read (`__nls_enc V`): functrace itself is in H.
    "functrace-after-h":
        "set -o functrace; trap 'if [[ $BASH_COMMAND == *__nls_enc\\ V* ]]; then"
        " trap - DEBUG; " + _BIND_ENTER_INNER + "; fi' DEBUG",
}


@pytest.mark.parametrize("attack", sorted(_H2_ATTACKS))
def test_code_run_after_done_cannot_bind_enter_unseen(start_session, monkeypatch, tmp_path,
                                                      attack):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    _try_run(session, _H2_ATTACKS[attack])
    assert session.degraded, "the next paste would be written with Enter bound to a macro"
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert session.degraded_code == "h-changed", session.degraded_reason


def test_chld_trap_cannot_bind_enter_after_the_snapshot(start_session, monkeypatch,
                                                         tmp_path):
    # The hooks' own `$( … )` children fire a CHLD trap, also after H is read: from the
    # fourth one on, this one binds Enter to a macro that would run `touch MARK`.
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    attack = "__k=0; trap '((++__k >= 4)) && " + _BIND_ENTER_INNER + "' CHLD"
    _assert_degrades_at_its_snapshot(session, attack, "h-changed")
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert not marker.exists()


def test_redefined_user_prompt_function_cannot_bind_enter_unseen(start_session, monkeypatch,
                                                                 tmp_path):
    _write_bashrc("f() { :; }\nPROMPT_COMMAND=f\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason
    marker = tmp_path / "MARK"
    _try_run(session, "f() { " + _BIND_ENTER_INNER + "; }")
    assert session.degraded
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert session.degraded_code == "h-changed", session.degraded_reason


# ---- (t) a signal trap, which runs while readline waits --


def test_signal_trap_cannot_bind_enter_while_readline_waits(start_session, monkeypatch,
                                                            tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    attack = "trap '" + _BIND_ENTER_INNER + "' USR1; (sleep 0.5; kill -USR1 $$) &"
    outcome = _try_run(session, attack)
    assert session.degraded, "the USR1 trap would bind Enter while readline waits"
    time.sleep(1.5)  # the trap has run by now
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert session.degraded_code == "h-changed", session.degraded_reason
    assert getattr(outcome, "code", None) == "h-changed", outcome


# ---- (k) R changed while readline holds the terminal (part 2) --
#
# The shell's own values of R are not checked (#149, decision 10): readline forces them
# while it reads a paste, so they cannot change its bytes. Only a change made while
# readline holds the terminal is caught, by the pre-paste reading.


def test_background_job_turning_signals_off_is_refused(start_session, monkeypatch):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    _run(session, "(trap '' TTOU; sleep 0.5; stty -isig) >/dev/null 2>&1 &")
    time.sleep(1.5)  # the job turned ISIG off while readline holds the terminal
    _refused_with_code(session, pty_writes, "echo hello", "outside-check")


def test_v5_fd4_bypass_shape_is_refused(start_session, monkeypatch, tmp_path):
    # v5's bypass took nl-shell's fd-4 go-ahead; there is no fd 4 now, and the job's
    # `stty -isig` while readline holds the terminal shows in the pre-paste reading.
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    _run(session, "(trap '' TTOU; sleep 0.5; stty -isig) >/dev/null 2>&1 &")
    time.sleep(1.5)
    _refused_with_code(session, pty_writes, "stty -a > MARK2", "outside-check")
    assert not (tmp_path / "MARK2").exists()
    _refused_with_code(session, pty_writes, "echo hello", "outside-check")


# ---- (j) the outside checks: terminal settings, TIOCSTI, a stopped shell --


def test_changed_interrupt_character_is_refused(start_session, monkeypatch, tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    _try_run(session, "stty intr '|'")
    _refused_with_code(session, pty_writes, "true || touch MARK", "outside-check",
                       tmp_path / "MARK")


@pytest.mark.parametrize("setting", ["istrip", "icrnl", "inlcr", "intr '|'"])
def test_background_job_changing_the_terminal_at_the_prompt_is_refused(start_session,
                                                                       monkeypatch, setting):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    result = _run(session, f"(trap '' TTOU; sleep 0.5; stty {setting}) &")
    assert result.ran and not session.degraded
    time.sleep(1.5)  # the job changed the settings while readline holds the terminal
    _refused_with_code(session, pty_writes, "echo hello", "outside-check")


def test_character_size_change_cannot_strip_paste_bytes(start_session):
    # Readline turns ISTRIP/INPCK off only with CS8 (or its meta flag): with CS7 a paste
    # would lose its eighth bit. A Linux PTY keeps CSIZE at CS8 whatever `stty` asks (the
    # kernel's pty_set_termios resets it), so `stty cs7 istrip` leaves only the shell's own
    # ISTRIP (not checked, #149), which readline clears while it reads: the next paste's
    # 8-bit bytes arrive intact. Any other CSIZE would differ from the first prompt's
    # reading and be refused before the paste (part 2).
    import termios

    session, _ = start_session()
    _try_run(session, "stty cs7 istrip")
    assert session.degraded or (
        _run(session, "printf '%s' 'é' | od -An -tx1").text.split() == ["c3", "a9"])
    assert termios.tcgetattr(session._proc.fd)[2] & termios.CSIZE == termios.CS8


@pytest.mark.parametrize("content", ["1\n", None], ids=["allowed", "missing"])
def test_tiocsti_not_off_starts_degraded(start_session, monkeypatch, tmp_path, content):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    path = tmp_path / "legacy_tiocsti"
    if content is not None:
        path.write_text(content, encoding="ascii")
    session, _ = start_session(tiocsti_path=str(path))
    assert session.degraded
    _refused_with_code(session, pty_writes, "echo hello", "tiocsti")
    assert str(path) in session.degraded_reason


def test_stopped_shell_degrades_instead_of_hanging(start_session):
    session_mod = _session_module()
    session, _ = start_session()
    began = time.monotonic()
    outcome = _run_default_timeout(session, "(sleep 0.3; kill -STOP $$) & sleep 2", limit=5.0)
    assert time.monotonic() - began < 5.0
    assert isinstance(outcome, session_mod.SessionDegraded), outcome
    assert outcome.code == "stopped", outcome.reason
    assert session.degraded_code == "stopped"


# ---- (fa) false-alarm guards --


def test_many_quick_commands_stay_healthy(start_session):
    session, _ = start_session()
    for _ in range(50):
        assert _run(session, "true").status == 0
    assert _run(session, "echo hello").text == "hello\n"
    assert not session.degraded, session.degraded_reason


def test_fd_check_at_e_waits_out_the_e_redirect(start_session, monkeypatch, tmp_path):
    # bash writes E with `printf 'E\n' >&3`; under CPU load the reading at E can land
    # before bash undoes that redirect, with fd 1 still the control pipe (round 6's false
    # alarm). Replay that: the first fd reading after E goes through a proc_root whose
    # fd/1 and fdinfo/1 are bash's real fd 3. A healthy session must stay healthy.
    session_cls = _session_module().Session
    session, _ = start_session()
    pid = str(session.pid)
    fake_root = tmp_path / "proc"
    for sub in ("fd", "fdinfo"):
        (fake_root / pid / sub).mkdir(parents=True)
        for fd, real_fd in ((0, 0), (1, 3), (2, 2)):
            os.symlink(f"/proc/{pid}/{sub}/{real_fd}", fake_root / pid / sub / str(fd))
    state = {"armed": False, "e_seen": False, "replayed": 0}
    real_read_control = session_cls._read_control
    real_read_fds = session_cls._read_fds

    def read_control(self):
        records = real_read_control(self)
        if state["armed"] and any(kind == "E" for kind, _ in records):
            state["e_seen"] = True
        return records

    def read_fds(self):
        if not (state["e_seen"] and state["replayed"] == 0):
            return real_read_fds(self)
        state["replayed"] += 1
        saved, self.proc_root = self.proc_root, str(fake_root)
        try:
            return real_read_fds(self)
        finally:
            self.proc_root = saved

    monkeypatch.setattr(session_cls, "_read_control", read_control)
    monkeypatch.setattr(session_cls, "_read_fds", read_fds)
    state["armed"] = True
    result = _try_run(session, "echo hello")
    assert state["replayed"] == 1, "the reading at E was never replayed"
    assert not session.degraded, session.degraded_reason
    assert result.text == "hello\n"
    assert _run(session, "echo again").text == "again\n"


# ---- (st) Python stalls must not degrade a healthy session --
#
# No check may depend on nl-shell being scheduled inside a window bash does not wait for
# (round 7's D..E reading did: any stall longer than a few ms degraded the session).


def _assert_stays_healthy_over_30_runs(session):
    assert not session.degraded, session.degraded_reason
    for _ in range(30):
        assert _run(session, "true").status == 0
    assert _run(session, "echo hello").text == "hello\n"
    assert not session.degraded, session.degraded_reason


def test_stall_after_each_pty_write_stays_healthy(start_session, monkeypatch):
    session_mod = _session_module()
    real_write_all = session_mod._write_all

    def slow_write_all(fd, data):
        real_write_all(fd, data)
        time.sleep(0.05)

    monkeypatch.setattr(session_mod, "_write_all", slow_write_all)
    session, _ = start_session()
    _assert_stays_healthy_over_30_runs(session)


def test_stall_before_each_control_read_stays_healthy(start_session, monkeypatch):
    session_cls = _session_module().Session
    real_read_control = session_cls._read_control

    def slow_read_control(self):
        time.sleep(0.05)
        return real_read_control(self)

    monkeypatch.setattr(session_cls, "_read_control", slow_read_control)
    session, _ = start_session()
    _assert_stays_healthy_over_30_runs(session)


def test_busy_python_thread_stays_healthy(start_session):
    stop = threading.Event()

    def spin() -> None:
        while not stop.is_set():
            pass

    busy = threading.Thread(target=spin, daemon=True)
    busy.start()  # before the session starts, so start-up runs under it too
    try:
        session, _ = start_session()
        _assert_stays_healthy_over_30_runs(session)
    finally:
        stop.set()
        busy.join(5.0)


# ---- (rm) the reading of the shell's own R (round 7's part 3) is gone (#149) --


def test_no_reading_of_the_shells_own_readline_fields():
    path = os.path.join(os.path.dirname(__file__), os.pardir, "src", "nl_shell", "session.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    for name in ("_read_shell_r", "_check_shell_r", "_shell_r"):
        assert name not in source, name


def test_outside_checks_do_not_degrade_ordinary_commands(start_session):
    session, _ = start_session()
    assert _run(session, "printf '%s' 'é' | od -An -tx1").text.split() == ["c3", "a9"]
    assert _run(session, "echo hello").text == "hello\n"
    assert not session.degraded, session.degraded_reason


def test_histsize_change_stays_healthy(start_session):
    session, _ = start_session()
    result = _run(session, "HISTSIZE=7")
    assert result.ran and not session.degraded
    assert _run(session, "echo hello").text == "hello\n"
    assert not session.degraded, session.degraded_reason


def test_bashrc_terminal_settings_are_the_baseline(start_session):
    _write_bashrc("stty -ixon\n")
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason
    for _ in range(3):
        assert _run(session, "echo hello").text == "hello\n"
    assert not session.degraded, session.degraded_reason


# ---- (i) I3 reproducers from ~/.bashrc --


def test_bashrc_prompt_command_binding_enter_on_its_second_run(start_session, monkeypatch,
                                                               tmp_path):
    _write_bashrc(
        "__pc_runs=0\n"
        "__pc() {\n"
        "  __pc_runs=$((__pc_runs + 1))\n"
        "  if [ \"$__pc_runs\" -eq 2 ]; then " + _BIND_ENTER + "; fi\n"
        "}\n"
        "PROMPT_COMMAND=__pc\n"
    )
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert not session.degraded
    marker = tmp_path / "MARK"
    _assert_degrades_at_its_snapshot(session, "true", "h-changed")
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    _assert_refused_unsent(session, pty_writes, "echo again", marker)


def test_bashrc_ps1_that_would_bind_enter_is_never_expanded(start_session, tmp_path):
    _write_bashrc("__ps1_bind() { " + _BIND_ENTER + "; }\nPS1='${ __ps1_bind; }\\$ '\n")
    marker = tmp_path / "MARK"
    session, _ = start_session()
    if session.degraded:
        assert session.degraded_reason
    else:
        assert _run(session, "echo hello").text == "hello\n"
        assert not session.degraded
    time.sleep(0.2)
    assert not marker.exists()


def test_bashrc_readonly_ps1_starts_degraded(start_session, monkeypatch, tmp_path):
    _write_bashrc("readonly PS1='$(" + _BIND_ENTER + ")\\$ '\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    marker = tmp_path / "MARK"
    session, _ = start_session()
    assert session.degraded
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert "PS1" in session.degraded_reason


# ---- (mail) bash's mail check runs after PROMPT_COMMAND, before readline --


def test_mail_check_is_off_for_good(start_session, monkeypatch, tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    result = _run(session, "MAILCHECK=0")
    assert result.status != 0 and "readonly" in result.text, result.text
    mailbox = tmp_path / "m"
    _run(session, f": > {mailbox}; MAILPATH='{mailbox}?$(stty -isig; touch MARK)'")
    _run(session, f"sleep 1.1; echo x >> {mailbox}")  # new mail: a check would run it
    pty_writes.clear()
    settings = _run(session, "stty -a")
    assert pty_writes, "the paste was written"
    assert "isig" in settings.text.split() and "-isig" not in settings.text.split()
    assert not session.degraded, session.degraded_reason
    time.sleep(0.2)
    assert not (tmp_path / "MARK").exists()


@pytest.mark.parametrize("name", ["MAILCHECK", "PS4"])
def test_bashrc_readonly_pinned_variable_starts_degraded(start_session, monkeypatch, name):
    _write_bashrc(f"readonly {name}=5\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert session.degraded
    _assert_refused_unsent(session, pty_writes, "echo hello")
    assert name in session.degraded_reason


@pytest.mark.parametrize(
    "ps4",
    ["'$(touch MARK)'", "'`touch MARK`'", r"'\044(touch MARK)'"],
    ids=["dollar", "backtick", "octal-escape"],
)
def test_bashrc_readonly_ps4_with_eval_disabled_starts_degraded(start_session, monkeypatch,
                                                                tmp_path, ps4):
    # No sentinel without eval: only the check on PS4's value can catch this.
    _write_bashrc(f"readonly PS4={ps4}\nenable -n eval\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert session.degraded
    assert "PS4 holds" in session.degraded_reason
    _assert_refused_unsent(session, pty_writes, "set -x", tmp_path / "MARK")


def test_bashrc_plain_ps4_still_gives_a_working_session(start_session):
    _write_bashrc("PS4='>> '\n")
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason
    assert "PS4: readonly variable" in _run(session, "PS4='$(touch MARK)'").text


def test_bashrc_function_named_like_a_sentinel_is_not_one(start_session):
    _write_bashrc("nl_shell_unpinned_helper() { :; }\n")
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason


@pytest.mark.parametrize(
    "bashrc",
    ["readonly() { :; }\n", "unset() { :; }\n", "alias readonly=: unset=:\n"],
    ids=["readonly-function", "unset-function", "aliases"],
)
def test_bashrc_shadowing_the_pins_does_not_stop_them(start_session, tmp_path, bashrc):
    _write_bashrc(bashrc)
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason
    assert "PS4: readonly variable" in _run(session, "PS4='$(touch MARK)'").text
    assert "MAILCHECK: readonly variable" in _run(session, "MAILCHECK=0").text
    assert _run(session, 'echo "${MAILCHECK-unset}"').text == "unset\n"
    time.sleep(0.2)
    assert not (tmp_path / "MARK").exists()


def test_bashrc_readonly_lc_all_still_gives_a_working_session(start_session):
    # __nls_enc's `declare LC_ALL=C` fails then; the records are still written.
    _write_bashrc("readonly LC_ALL=C.UTF-8\n")
    session, _ = start_session()
    assert not session.degraded, session.degraded_reason
    assert _run(session, "echo one").text == "one\n"
    assert _run(session, "echo two").text == "two\n"
    assert not session.degraded, session.degraded_reason


@pytest.mark.parametrize("name", ["readonly", "unset"])
def test_bashrc_disabling_a_pin_builtin_starts_degraded(start_session, monkeypatch,
                                                        tmp_path, name):
    _write_bashrc(f"enable -n {name}\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert session.degraded
    assert "not pinned" in session.degraded_reason
    _assert_refused_unsent(session, pty_writes, "PS4='$(touch MARK)'; set -x",
                           tmp_path / "MARK")


# ---- (m, second half) and (p): readline settings from ~/.bashrc and after a command --


def test_bashrc_vi_mode_and_bracketed_paste_off_still_work(start_session):
    _write_bashrc("bind 'set enable-bracketed-paste off'\nset -o vi\n")
    session, records = start_session()
    assert not session.degraded, session.degraded_reason
    result = _run(session, "echo one\necho two")
    assert result.text == "one\ntwo\n"
    assert _pairs(records.for_command(1)) == (1, 1)
    assert not session.degraded


@pytest.mark.parametrize(
    "change",
    ["bind 'set enable-bracketed-paste off'", "set +o emacs", "set -o vi"],
    ids=["bracketed-paste-off", "readline-off", "vi-mode"],
)
def test_readline_change_never_gives_empty_healthy_output(start_session, change):
    session_mod = _session_module()
    session, _ = start_session()
    _try_run(session, change)
    after = _try_run(session, "echo hello")
    if isinstance(after, session_mod.CommandResult) and not session.degraded:
        assert after.output != b""
        assert after.text == "hello\n"
    else:
        assert session.degraded


# ---- (r) PS4 and xtrace --


def test_xtrace_to_another_fd_with_ps4_code_never_runs_it(start_session, monkeypatch,
                                                          tmp_path):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    caught = _assert_degrades_at_its_snapshot(
        session, "exec 9>/dev/null; BASH_XTRACEFD=9; PS4='$(echo x >> MARK)'; set -x",
        "h-changed")
    assert "PS4: readonly variable" in caught.result.text
    _assert_refused_unsent(session, pty_writes, "echo hello", marker)
    assert not marker.exists()


@pytest.mark.parametrize(
    "command",
    ["PS4='$(touch MARK)'", "declare PS4='$(touch MARK)'", "printf -v PS4 '$(touch MARK)'"],
    ids=["assign", "declare", "printf-v"],
)
def test_ps4_is_readonly(start_session, tmp_path, command):
    session, _ = start_session()
    result = _run(session, command)
    assert result.status != 0
    assert "readonly" in result.text
    assert not session.degraded
    assert _run(session, "echo hello").text == "hello\n"
    time.sleep(0.2)
    assert not (tmp_path / "MARK").exists()


@pytest.mark.parametrize(
    "command, changed, before",
    [
        ("set -x", "set -o xtrace", "set +o xtrace"),
        ("set -o xtrace", "set -o xtrace", "set +o xtrace"),
        ("exec 9>/dev/null; BASH_XTRACEFD=9", 'declare -- BASH_XTRACEFD="9"',
         "BASH_XTRACEFD unset"),
    ],
    ids=["set-x", "set-o-xtrace", "xtracefd"],
)
def test_xtrace_settings_degrade_naming_their_h_line(start_session, monkeypatch, command,
                                                     changed, before):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    baseline_lines = session.baseline.hooks.split("\n")
    assert before in baseline_lines and changed not in baseline_lines
    caught = _assert_degrades_at_its_snapshot(session, command, "h-changed")
    lines = caught.result.snapshot.hooks.split("\n")
    assert changed in lines and before not in lines
    _assert_refused_unsent(session, pty_writes, "echo hello")


# ---- (s) the hook builtins --


def _hook_part(rcfile: str) -> str:
    """The hook part of the rcfile: the bodies of the `__nls_*` functions, the PS0 line and
    the PROMPT_COMMAND line (not the lines that run only while the rcfile loads)."""
    lines = rcfile.split("\n")
    first = next(i for i, line in enumerate(lines) if re.match(r"__nls_\w+\(\) \{", line))
    last = next(i for i, line in enumerate(lines) if line == "\\builtin unset PROMPT_COMMAND")
    part = [line for line in lines if line.startswith(("PS0=", "PROMPT_COMMAND=("))]
    return "\n".join(part + lines[first:last])


def test_hook_builtins_are_exactly_the_ones_the_hooks_call():
    session_mod = _session_module()
    part = _hook_part(session_mod.RCFILE)
    assert "__nls_snap() {" in part and "\\builtin unset" not in part
    called = set(re.findall(r"\\?builtin ([A-Za-z_]+|:)", part)) | {"builtin"}
    assert set(session_mod.HOOK_BUILTINS) == called, (
        set(session_mod.HOOK_BUILTINS) ^ called)
    assert len(session_mod.HOOK_BUILTINS) == len(set(session_mod.HOOK_BUILTINS))


# Disabling these stops the hooks writing records at all: every record is written by
# `\builtin printf`, and both hook functions start with `\builtin set` and return if it
# fails. The prompt rule then reports the missing done record.
_NO_RECORD_BUILTINS = ("builtin", "printf", "set")


def test_disabling_any_hook_builtin_degrades(start_session, monkeypatch):
    session_mod = _session_module()
    pty_writes = _spy_on_pty_writes(monkeypatch)
    for name in session_mod.HOOK_BUILTINS:
        session, _ = start_session()
        began = time.monotonic()
        outcome = _run_default_timeout(session, f"enable -n {name}", limit=5.0)
        assert time.monotonic() - began < 5.0, name
        assert isinstance(outcome, session_mod.SessionDegraded), (name, outcome)
        _assert_refused_unsent(session, pty_writes, "echo hello")
        if name in _NO_RECORD_BUILTINS:
            expected = "missing-record"
        else:
            expected = "h-changed"
        assert outcome.code == expected, (name, outcome.code, outcome.reason)
        session.close()


@pytest.mark.parametrize(
    "setup, command",
    [
        ("trap '[[ $BASH_COMMAND == \"echo later\" ]] && : > MARK' DEBUG; enable -n trap",
         "echo later"),
        ("alias ls='touch MARK #'; enable -n alias", "ls"),
    ],
    ids=["hidden-debug-trap", "hidden-alias"],
)
def test_disabling_the_builtin_that_would_show_a_change_is_refused(start_session, monkeypatch,
                                                                   tmp_path, setup, command):
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    marker = tmp_path / "MARK"
    _try_run(session, setup)
    assert session.degraded, "refused or degraded before the paste"
    _assert_refused_unsent(session, pty_writes, command, marker)
    assert not marker.exists()


@pytest.mark.parametrize("setting", ["set -T", "shopt -s extdebug"], ids=["T", "extdebug"])
def test_bashrc_functrace_starts_degraded(start_session, monkeypatch, setting):
    # Under functrace a DEBUG trap is inherited by the substitution that reads the traps
    # and could hide itself there, then run inside the hooks after H.
    _write_bashrc(setting + "\n")
    pty_writes = _spy_on_pty_writes(monkeypatch)
    session, _ = start_session()
    assert session.degraded
    _assert_refused_unsent(session, pty_writes, "echo hello")
    assert "functrace" in session.degraded_reason
