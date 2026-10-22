"""Terminal ownership while a command runs (#102), pty-protocol v1 §6.

A real bash (>= 5.1) runs the commands; an ``os.openpty()`` pair stands in for the user's
real terminal: nl-shell gets its slave, and the test types on its master and reads from it
what nl-shell mirrors. The terminal module is imported inside each test, so without it
each test fails on its own. Every wait has a timeout; every session and stand-in terminal
is closed in teardown. Section letters are the items of #102's success criterion.
"""

from __future__ import annotations

import fcntl
import gc
import importlib
import logging
import os
import select
import signal
import struct
import sys
import termios
import threading
import time
import types

import pytest

START_TIMEOUT = 5.0
RUN_TIMEOUT = 10.0


def _session_module():
    return importlib.import_module("nl_shell.session")


def _terminal_module():
    return importlib.import_module("nl_shell.session_terminal")


class _StandIn:
    """An ``os.openpty()`` pair as the real terminal. ``fd`` (the slave) is what nl-shell
    gets; the test types on the master, and a thread keeps reading the master into
    ``output`` (what reached the screen), as a real terminal would."""

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.master, self.fd = os.openpty()
        self.resize(rows, cols)
        os.set_blocking(self.master, False)
        self.output = bytearray()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _read(self) -> None:
        while not self._closing.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            with self._lock:
                self.output += data

    def screen(self) -> bytes:
        with self._lock:
            return bytes(self.output)

    def wait_for_screen(self, text: bytes, limit: float = 3.0) -> bytes:
        deadline = time.monotonic() + limit
        while text not in self.screen():
            assert time.monotonic() < deadline, f"{text!r} never reached the screen"
            time.sleep(0.01)
        return self.screen()

    def type(self, data: bytes, limit: float = 3.0) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + limit
        while view:
            assert time.monotonic() < deadline, "the stand-in terminal stopped taking input"
            select.select([], [self.master], [], 0.05)
            try:
                view = view[os.write(self.master, view) :]
            except BlockingIOError:
                continue

    def close(self) -> None:
        self._closing.set()
        self._reader.join(2.0)
        for fd in (self.master, self.fd):
            try:
                os.close(fd)
            except OSError:
                pass


class _Later:
    """Run ``action`` in a thread after ``delay`` seconds; ``join`` is bounded."""

    def __init__(self, delay: float, action) -> None:
        self.when: float | None = None
        self.error: BaseException | None = None

        def body() -> None:
            time.sleep(delay)
            try:
                action()
            except BaseException as exc:  # handed back to the test in join()
                self.error = exc
            self.when = time.monotonic()

        self._thread = threading.Thread(target=body, daemon=True)
        self._thread.start()

    def join(self, limit: float = 5.0) -> None:
        self._thread.join(limit)
        assert not self._thread.is_alive(), "helper thread still running"
        if self.error is not None:
            raise self.error


@pytest.fixture
def start_session(monkeypatch, tmp_path):
    monkeypatch.delenv("VTE_VERSION", raising=False)
    started = []

    def start(**kwargs):
        kwargs.setdefault("cwd", str(tmp_path))
        session = _session_module().Session.start(timeout=START_TIMEOUT, **kwargs)
        started.append(session)
        return session

    yield start
    for session in started:
        session.close()


@pytest.fixture
def stand_in():
    made = []

    def make(rows: int = 24, cols: int = 80) -> _StandIn:
        terminal = _StandIn(rows, cols)
        made.append(terminal)
        return terminal

    yield make
    for terminal in made:
        terminal.close()


def _write_bashrc(text: str) -> None:
    with open(os.path.join(os.environ["HOME"], ".bashrc"), "w", encoding="utf-8") as fh:
        fh.write(text)


def _run(session, command, terminal, timeout=RUN_TIMEOUT):
    return session.run(command, timeout=timeout, terminal=terminal)


# ---- (a) vector 11: the alternate screen is mirrored, not captured --


def test_alternate_screen_is_mirrored_but_not_captured(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    command = (
        r"printf 'A1\n\033[?1049hHIDDEN1\033[?1049lA2\n'; "
        r"printf '\033[?47hHIDDEN2\033[?47lA3\n'; "
        r"printf '\033[?1047hHIDDEN3\033[?1047lA4\n'; "
        # The mode among other parameters, with leading zeros, and after 8-bit CSI (0x9b):
        # the terminal switches screens for all of them, so capture must stop too.
        r"printf '\033[?25;1049hHIDDEN6\033[?1049;25lA5\n'; "
        r"printf '\033[?01047hHIDDEN7\033[?1047lA6\n'; "
        r"printf '\233?47hHIDDEN8\233?47lA7\n'; "
        r"printf '\033[?25lA8\033[?25h\n'; "
        # 0x9b inside a UTF-8 character is text, not CSI: U+00DB (C3 9B) and U+045B (D1 9B)
        # neither stop capture nor resume it on the alternate screen. C2 9B is U+009B, the
        # CSI itself.
        r"printf 'B1\303\233?1049hB2\321\233?47lB3\n'; "
        r"printf '\033[?47hHIDDEN9\321\233?47lHIDDEN10\033[?47lA9\n'; "
        r"printf '\033[?1049hHIDDEN11\303\233?1049lHIDDEN12\033[?1049lA10\n'; "
        r"printf '\302\233?47hHIDDEN13\302\233?47lA11\n'; "
        r"printf '\033[?1049hHIDDEN4\033[?47lHIDDEN5\n'"
    )
    result = _run(session, command, terminal_mod.Terminal(screen.fd))
    assert result.ran and result.status == 0
    assert not result.input_forwarded and not result.mirror_dropped
    # Capture resumes after each matching reset; `?1049h` closed by `?47l` stays paused.
    # Private modes that are not the alternate screen are plain output.
    expected = (
        "A1\nA2\nA3\nA4\nA5\nA6\nA7\n\x1b[?25lA8\x1b[?25h\n"
        "B1\u00db?1049hB2\u045b?47lB3\nA9\nA10\nA11\n"
    )
    assert result.text == expected, result.text
    shown = screen.wait_for_screen(b"HIDDEN5")
    for text in (b"A1", b"HIDDEN1", b"HIDDEN2", b"HIDDEN3", b"HIDDEN4", b"A4",
                 b"HIDDEN6", b"HIDDEN7", b"HIDDEN8", b"A8", b"HIDDEN10", b"HIDDEN12",
                 b"HIDDEN13", b"B3"):
        assert text in shown, text
    assert b"\x1b[?1049h" in shown and b"\x1b[?1047l" in shown
    assert not session.degraded


def test_utf8_text_is_not_read_as_8_bit_csi_when_split_across_reads():
    # The PTY may hand output over in any pieces, so a UTF-8 character can be cut between
    # its lead byte and the 0x9b. Feed the capture filter one byte at a time.
    terminal_mod = _terminal_module()
    cases = [
        (b"A\xc3\x9b?1049hB\n", b"A\xc3\x9b?1049hB\n"),
        (b"\x1b[?47hX\xd1\x9b?47lY\x1b[?47lZ", b"Z"),
        (b"\x1b[?1049hX\xe2\x80\x9b?1049lY\x1b[?1049lZ", b"Z"),
        (b"A\xc2\x9b?47hX\xc2\x9b?47lB", b"AB"),
        (b"A\x9b?47hX\x9b?47lB", b"AB"),
    ]
    for data, captured in cases:
        whole = terminal_mod._AltScreen()
        assert whole.filter(data) + whole.flush() == captured, data
        split = terminal_mod._AltScreen()
        out = b"".join(split.filter(data[i : i + 1]) for i in range(len(data)))
        assert out + split.flush() == captured, data


# ---- (b) vector 10 via forwarding: Ctrl-C typed on the real terminal --


def test_ctrl_c_typed_on_the_terminal_interrupts_the_command(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    typed = _Later(0.5, lambda: screen.type(b"\x03"))
    result = _run(session, "sleep 5", terminal_mod.Terminal(screen.fd))
    returned = time.monotonic()
    typed.join()
    assert result.ran and result.status == 130, result
    assert typed.when is not None and returned - typed.when < 2.0
    assert not session.degraded
    assert _run(session, "echo ok", terminal_mod.Terminal(screen.fd)).text == "ok\n"


# ---- (c) forwarded input is never logged, captured or kept --


def _holds(root, needles: tuple[bytes, str], seen: set[int], path: str, found: list[str],
           depth: int = 0) -> None:
    """Record in ``found`` every path from ``root`` to a value holding a needle."""
    if depth > 40 or id(root) in seen:
        return
    seen.add(id(root))
    raw, text = needles
    if isinstance(root, (bytes, bytearray, memoryview)):
        if raw in bytes(root):
            found.append(path)
        return
    if isinstance(root, str):
        if text in root:
            found.append(path)
        return
    if isinstance(root, (types.ModuleType, type)) and depth > 0:
        return  # other modules and classes are not state of this run
    if isinstance(root, dict):
        for key, value in list(root.items()):
            _holds(key, needles, seen, f"{path}[key]", found, depth + 1)
            _holds(value, needles, seen, f"{path}[{key!r}]", found, depth + 1)
        return
    if isinstance(root, (list, tuple, set, frozenset)):
        for i, value in enumerate(list(root)):
            _holds(value, needles, seen, f"{path}[{i}]", found, depth + 1)
        return
    if isinstance(root, types.MethodType):
        _holds(root.__self__, needles, seen, f"{path}.__self__", found, depth + 1)
        _holds(root.__func__, needles, seen, f"{path}.__func__", found, depth + 1)
        return
    if isinstance(root, types.FunctionType):
        for i, cell in enumerate(root.__closure__ or ()):
            try:
                contents = cell.cell_contents
            except ValueError:
                continue
            _holds(contents, needles, seen, f"{path}.<cell {i}>", found, depth + 1)
        _holds(root.__defaults__, needles, seen, f"{path}.__defaults__", found, depth + 1)
        _holds(root.__kwdefaults__, needles, seen, f"{path}.__kwdefaults__", found, depth + 1)
        return
    attrs = getattr(root, "__dict__", None)
    if isinstance(attrs, dict):
        _holds(attrs, needles, seen, f"{path}.__dict__", found, depth + 1)
    for slot in getattr(type(root), "__slots__", ()):
        if hasattr(root, slot):
            _holds(getattr(root, slot), needles, seen, f"{path}.{slot}", found, depth + 1)


def test_forwarded_input_reaches_the_program_and_is_kept_nowhere(start_session, stand_in,
                                                                 monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)  # every log record, on the root logger
    terminal_mod = _terminal_module()
    session_mod = _session_module()
    created: list[object] = []
    real_own = terminal_mod.Terminal.own

    def own(self, master_fd):
        holder = real_own(self, master_fd)
        created.append(holder)
        return holder

    monkeypatch.setattr(terminal_mod.Terminal, "own", own)
    sentinel = b"qz-forwarded-" + os.urandom(6).hex().encode()
    session = start_session()
    screen = stand_in()
    terminal = terminal_mod.Terminal(screen.fd)
    # `cat` ends only if the line and the Ctrl-D reach it: the bytes reached the program.
    typed = _Later(0.5, lambda: screen.type(sentinel + b"\n\x04"))
    result = _run(session, "cat > /dev/null; echo cat-ended", terminal)
    typed.join()
    assert result.ran and result.status == 0
    assert result.input_forwarded
    assert not session.degraded
    screen.wait_for_screen(b"cat-ended")

    needles = (sentinel, sentinel.decode())
    objects = [obj for obj in gc.get_objects()
               if type(obj).__module__ == "nl_shell.session_terminal"]
    roots = {
        "result": result,
        "session": session,
        "terminal": terminal,
        "created": created,
        "live terminal objects": objects,
        "nl_shell.session globals": vars(session_mod),
        "nl_shell.session_terminal globals": vars(terminal_mod),
    }
    assert created and objects
    found: list[str] = []
    for name, root in roots.items():
        _holds(root, needles, set(), name, found)
    assert found == [], found
    for record in caplog.records:
        assert needles[1] not in record.getMessage(), record
        assert needles[1] not in repr(record.args), record
        assert needles[1] not in repr(vars(record)), record


def test_bytes_typed_after_the_done_record_are_not_forwarded(start_session, stand_in):
    # The window ends at D (§6.1). The user's PROMPT_COMMAND from ~/.bashrc runs after D
    # and before the snapshot, so a slow one leaves time to type between D and E.
    _write_bashrc("PROMPT_COMMAND='sleep 0.8'\n")
    terminal_mod = _terminal_module()
    done = threading.Event()

    def on_record(line: bytes) -> None:
        if line.startswith(b"D "):
            done.set()

    session = start_session(on_record=on_record)
    screen = stand_in()
    done.clear()

    def type_after_done() -> None:
        assert done.wait(5.0), "no D record"
        time.sleep(0.2)
        screen.type(b"k")

    typed = _Later(0.0, type_after_done)
    result = _run(session, "true", terminal_mod.Terminal(screen.fd))
    typed.join()
    assert result.status == 0
    assert not result.input_forwarded
    assert not session.degraded


def test_bytes_typed_before_the_run_are_not_forwarded(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    screen.type(b"Z\n")  # a whole line, waiting in the stand-in before run() is called
    time.sleep(0.1)
    result = _run(session, 'timeout --foreground 1 head -c 1; echo "rc=$?"',
                  terminal_mod.Terminal(screen.fd))
    assert not result.input_forwarded
    assert result.text == "rc=124\n", result.text
    assert not session.degraded


# ---- (d) decision 6: typed-ahead input never joins the next command --


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_typed_ahead_input_does_not_join_the_next_command(start_session, stand_in, mode):
    if mode == "vi":
        _write_bashrc("set -o vi\n")
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    terminal = terminal_mod.Terminal(screen.fd)
    typed = _Later(0.5, lambda: screen.type(b"echo INJECTED; "))
    first = _run(session, "sleep 2", terminal)
    typed.join()
    assert first.status == 0 and first.input_forwarded
    after = _run(session, "true", terminal)
    assert (after.ran, after.status, after.text) == (True, 0, ""), after
    assert not session.degraded


def test_a_typed_ahead_burst_does_not_change_the_next_command(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    terminal = terminal_mod.Terminal(screen.fd)
    stop = threading.Event()
    sent = [0]
    burst = b"x" * (200 * 1024)

    def write_burst() -> None:  # the stand-in's input can fill: never block on it
        view = memoryview(burst)
        while view and not stop.is_set():
            select.select([], [screen.master], [], 0.05)
            try:
                count = os.write(screen.master, view[:4096])
            except BlockingIOError:
                continue
            sent[0] += count
            view = view[count:]

    def start_burst() -> None:
        threading.Thread(target=write_burst, daemon=True).start()

    writer = _Later(0.3, start_burst)
    began = time.monotonic()
    first = _run(session, "sleep 2", terminal)
    assert time.monotonic() - began < RUN_TIMEOUT
    stop.set()
    writer.join()
    time.sleep(0.1)  # the writer thread sees `stop`
    assert first.status == 0 and first.input_forwarded
    assert sent[0] > 4096, sent[0]  # more than a tty line buffer holds
    after = _run(session, "true", terminal)
    assert (after.ran, after.status, after.text) == (True, 0, ""), after
    assert not session.degraded


def test_a_full_input_buffer_does_not_stall_the_run(start_session, stand_in):
    # A PTY takes ~18 KB of input before a write blocks (in non-canonical mode; in canonical
    # mode the line discipline drops what a full line buffer cannot hold). Here the command
    # never reads its input and writes 300 KB: a forwarder blocked on a full input buffer would stop
    # reading the PTY, the command would block on its output, and D would never come.
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    terminal = terminal_mod.Terminal(screen.fd)
    stop = threading.Event()
    burst = b"x" * (200 * 1024)

    def write_burst() -> None:
        view = memoryview(burst)
        while view and not stop.is_set():
            select.select([], [screen.master], [], 0.05)
            try:
                view = view[os.write(screen.master, view[:4096]) :]
            except BlockingIOError:
                continue

    writer = threading.Thread(target=write_burst, daemon=True)
    outcome: list[object] = []

    def run() -> None:  # off the main thread, so a deadlock fails the test, not the suite
        try:
            outcome.append(_run(session, "stty -icanon; sleep 1; head -c 300000 /dev/zero"
                                         " | tr '\\0' y; stty icanon; echo end", terminal))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.3)
    writer.start()
    worker.join(RUN_TIMEOUT + 2)
    stop.set()
    writer.join(2.0)
    assert not worker.is_alive(), "run() stalled behind a full PTY input buffer"
    result = outcome[0]
    assert not isinstance(result, Exception), result
    assert result.status == 0 and result.input_forwarded
    after = _run(session, "true", terminal)
    assert (after.ran, after.status, after.text) == (True, 0, ""), after
    assert not session.degraded


@pytest.mark.parametrize("blocking", [True, False])
def test_a_terminal_that_stops_reading_does_not_stall_the_run(start_session, stand_in,
                                                              blocking):
    # The real terminal's output side is a pipe nobody reads, already full. Copying the
    # command's output there must not keep the session from reading D and E (or from
    # timing out): the copy is dropped, the capture is whole.
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    out_r, out_w = os.pipe()
    os.set_blocking(out_w, False)
    try:
        while True:
            os.write(out_w, b"\0" * 65536)
    except BlockingIOError:
        pass
    os.set_blocking(out_w, blocking)
    terminal = terminal_mod.Terminal(screen.fd, out_w)
    outcome: list[object] = []

    def run() -> None:  # off the main thread, so a stall fails the test, not the suite
        try:
            outcome.append(_run(session, "head -c 100000 /dev/zero | tr '\\0' y; echo end",
                                terminal))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        worker.join(RUN_TIMEOUT + 2)
        stalled = worker.is_alive()
    finally:
        os.set_blocking(out_r, False)
        deadline = time.monotonic() + 5.0
        while worker.is_alive() and time.monotonic() < deadline:  # free a stalled run
            try:
                os.read(out_r, 65536)
            except BlockingIOError:
                time.sleep(0.01)
    try:
        assert not stalled, "run() stalled behind a real terminal that stopped reading"
        result = outcome[0]
        assert not isinstance(result, Exception), result
        assert result.status == 0 and not result.input_forwarded
        assert result.text == "y" * 100000 + "end\n"
        assert result.mirror_dropped  # the user never saw that output: the result says so
        assert os.get_blocking(out_w) is blocking
        assert not session.degraded
    finally:
        os.close(out_r)
        os.close(out_w)


def test_output_to_a_closed_terminal_is_reported_dropped(start_session, stand_in):
    """The real terminal's output goes away (EPIPE): the result must say the user saw
    nothing, not that every byte was mirrored."""
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    out_r, out_w = os.pipe()
    os.close(out_r)  # every write to out_w now fails
    try:
        result = _run(session, "echo hello", terminal_mod.Terminal(screen.fd, out_w))
        assert result.ran and result.status == 0
        assert result.text == "hello\n"
        assert result.mirror_dropped
        assert not session.degraded
    finally:
        os.close(out_w)


def test_the_terminal_fds_keep_their_flags_while_a_command_runs(start_session, stand_in):
    # O_NONBLOCK lives in the open file description that every copy of the terminal's fd
    # shares (the login shell, its background jobs). nl-shell must not set it there, not
    # even for the length of a command.
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    other = os.dup(screen.fd)  # another process's copy of the same terminal
    seen: list[tuple[bool, bool]] = []
    try:
        probe = _Later(0.4, lambda: seen.append((os.get_blocking(screen.fd),
                                                  os.get_blocking(other))))
        result = _run(session, "echo x; sleep 1", terminal_mod.Terminal(screen.fd))
        probe.join()
        assert result.status == 0 and result.text == "x\n"
        assert seen == [(True, True)], seen
        assert os.get_blocking(screen.fd) and os.get_blocking(other)
        screen.wait_for_screen(b"x")  # the copy still reaches the terminal
    finally:
        os.close(other)


def test_the_drain_keeps_a_command_fast_without_a_terminal(start_session):
    session = start_session()
    session.run("true", timeout=RUN_TIMEOUT)
    began = time.monotonic()
    result = session.run("true", timeout=RUN_TIMEOUT)
    assert time.monotonic() - began < 0.5
    assert result.status == 0 and not result.input_forwarded
    # The drain needs `read`: disabling it degrades the session (H, HOOK_BUILTINS).
    assert "read" in _session_module().HOOK_BUILTINS


# ---- (e) decision 7: capture stops at the first forwarded byte --


def test_capture_stops_at_the_first_forwarded_byte(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    terminal = terminal_mod.Terminal(screen.fd)
    command = "printf 'BEFORE\\n'; sleep 1; printf 'AFTER\\n'"
    quiet = _run(session, command, terminal)
    assert not quiet.input_forwarded and quiet.text == "BEFORE\nAFTER\n"
    typed = _Later(0.5, lambda: screen.type(b"k"))
    result = _run(session, command, terminal)
    typed.join()
    assert result.input_forwarded
    assert "BEFORE" in result.text and "AFTER" not in result.text, result.text
    assert result.text.count("AFTER") == 0
    screen.wait_for_screen(b"BEFORE\r\nk")  # the echo and the rest still reach the screen
    screen.wait_for_screen(b"AFTER")
    assert not session.degraded


# ---- (f) SIGWINCH copies the real terminal's size to the PTY --


def test_window_size_change_reaches_the_pty(start_session, stand_in):
    assert threading.current_thread() is threading.main_thread()
    terminal_mod = _terminal_module()
    session = start_session(dimensions=(24, 80))
    screen = stand_in(24, 80)

    def marker(signum, frame):
        pass

    previous = signal.signal(signal.SIGWINCH, marker)
    try:
        def resize() -> None:
            screen.resize(40, 100)
            os.kill(os.getpid(), signal.SIGWINCH)

        later = _Later(0.3, resize)
        result = _run(session, "sleep 1; stty size", terminal_mod.Terminal(screen.fd))
        later.join()
        assert result.text == "40 100\n", result.text
        assert signal.getsignal(signal.SIGWINCH) is marker
    finally:
        signal.signal(signal.SIGWINCH, previous)
    assert not session.degraded


# ---- (g) the real terminal's settings are put back on every way out --


def test_terminal_settings_restored_after_a_normal_run(start_session, stand_in):
    terminal_mod = _terminal_module()
    session = start_session()
    screen = stand_in()
    before = termios.tcgetattr(screen.fd)
    _run(session, "true", terminal_mod.Terminal(screen.fd))
    assert termios.tcgetattr(screen.fd) == before
    assert os.get_blocking(session._proc.fd)


def test_terminal_settings_restored_after_a_degraded_run(start_session, stand_in):
    terminal_mod = _terminal_module()
    session_mod = _session_module()
    session = start_session()
    screen = stand_in()
    before = termios.tcgetattr(screen.fd)
    with pytest.raises(session_mod.SessionDegraded):
        _run(session, "PROMPT_COMMAND=:", terminal_mod.Terminal(screen.fd))
    assert session.degraded
    assert termios.tcgetattr(screen.fd) == before


class _Boom(Exception):
    pass


def test_terminal_settings_restored_after_an_exception_mid_command(start_session, stand_in):
    terminal_mod = _terminal_module()
    armed = [False]

    def on_record(line: bytes) -> None:
        if armed[0] and line.startswith(b"S "):
            raise _Boom("raised while the command runs")

    session = start_session(on_record=on_record)
    screen = stand_in()
    before = termios.tcgetattr(screen.fd)
    armed[0] = True
    began = time.monotonic()
    with pytest.raises(_Boom):
        _run(session, "sleep 1", terminal_mod.Terminal(screen.fd))
    assert time.monotonic() - began < 1.0  # raised during the command, not after it
    assert termios.tcgetattr(screen.fd) == before
    assert os.get_blocking(session._proc.fd)
    assert sys.exc_info() == (None, None, None)
