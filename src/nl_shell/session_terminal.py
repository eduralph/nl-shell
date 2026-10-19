"""Terminal ownership while a command runs (pty-protocol v1 §6, ADR-0014 Decision 4).

``Session.run(command, terminal=Terminal(fd))`` hands the user's real terminal to the
command between sending the paste and its ``D`` record:

- the real terminal is put in raw mode and every byte typed on it goes to the PTY master
  unchanged; no forwarded byte is logged, captured or kept (§6.1). Bytes typed before the
  paste are discarded, and bytes still queued in nl-shell when ``D`` arrives are dropped;
- PTY output is copied to the real terminal and captured, except on the alternate screen:
  capture stops on ``ESC [?1049h``, ``ESC [?47h`` or ``ESC [?1047h`` and resumes only on
  the reset of the same mode, wherever the mode appears in the sequence (§6.2). The copy
  never blocks the session: it is queued, written through a non-blocking description of
  nl-shell's own (the terminal's shared fds keep their flags) only when the real terminal
  takes it, and dropped past ``MIRROR_LIMIT`` or when the terminal stops reading, so
  ``D``, ``E`` and the caller's timeout still come; ``CommandResult.mirror_dropped``
  says when that happened;
- on ``SIGWINCH`` the real terminal's size is copied to the PTY (§6.3);
- control keys travel as bytes (§6.4): raw mode turns the real terminal's own signal keys
  off, so Ctrl-C reaches the PTY's line discipline as ``\\x03``;
- the real terminal's settings are put back on every exit path (§6.5).

Once a byte has been forwarded, the rest of that command's output is not captured, and
``CommandResult.input_forwarded`` says so: the output now holds the echo of what was
typed (decision 7 of #10). Forwarded input that the command did not read is discarded by
the prompt hook before bash reads its next line (decision 6, the drain in
``nl_shell.session.RCFILE``).

The real terminal is given by its fds, so tests can use an ``os.openpty()`` pair. Only
the standard library is imported here.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import stat
import struct
import termios
import threading
import time
import tty

__all__ = ["Terminal"]

# Forwarded input goes to the session master in writes of at most this many bytes, and the
# control channel is checked before every one of them (the brief of #102).
CHUNK = 1024

# The copy of PTY output waiting for the real terminal (§6.2) holds at most this much;
# more is dropped (the capture keeps it). At the end of a run the rest is written for at
# most MIRROR_SETTLE seconds, and only while the terminal keeps taking bytes.
MIRROR_LIMIT = 1 << 20
MIRROR_SETTLE = 1.0
_MIRROR_STALL = 0.25
# Without a description of its own (see ``_open_private``) the copy is written only when
# ``select`` says the terminal has room, and at most this much at a time.
_SHARED_CHUNK = 256

# The alternate-screen modes (xterm: 1049 saves the cursor and clears, 47 and 1047 do not).
# A terminal acts on them anywhere in a DEC private mode sequence: alone (``ESC [?1049h``),
# among other parameters (``ESC [?25;1049h``), with leading zeros (``ESC [?01049h``), or
# after the 8-bit CSI ``0x9b`` where the terminal accepts C1 codes. So the whole parameter
# list is parsed. In UTF-8 text ``0x9b`` is also a continuation byte (``Û`` is C3 9B), so a
# ``0x9b`` inside a character is text, not CSI (see ``_c1_csi_at``).
_ALT_MODES = frozenset({47, 1047, 1049})
# C2 9B (U+009B in UTF-8) is taken whole, so its lead byte is not left in the capture.
_PRIVATE_MODE = re.compile(rb"(?:\x1b\[|\xc2?\x9b)\?([0-9;]*)([hl])")
# The end of a read that may be the start of such a sequence, split across reads.
_PRIVATE_MODE_START = re.compile(
    rb"(?:\x1b(?:\[(?:\?[0-9;]*)?)?|\xc2?\x9b(?:\?[0-9;]*)?|\xc2)\Z"
)
# At most this much is held back waiting for the rest of a split sequence.
_HOLD_LIMIT = 4096


def _c1_csi_at(text: bytes, i: int) -> bool:
    """Whether the ``0x9b`` at ``text[i]`` is the 8-bit CSI and not part of a UTF-8
    character. Inside a character (``Û`` = C3 9B, ``ћ`` = D1 9B, or a 3- or 4-byte one) it
    is text. C2 9B is U+009B, the CSI itself, and a stray ``0x9b`` is the raw C1 code."""
    for back in (1, 2, 3):
        if i - back < 0:
            return True
        byte = text[i - back]
        if 0x80 <= byte <= 0xBF:
            continue  # another continuation byte: look further back for the lead byte
        if 0xC2 <= byte <= 0xDF:
            size = 2
        elif 0xE0 <= byte <= 0xEF:
            size = 3
        elif 0xF0 <= byte <= 0xF4:
            size = 4
        else:
            return True  # ASCII or a byte that never leads: the 0x9b stands alone
        return size <= back or byte == 0xC2
    return True


def _held_tail(buf: bytes, before: bytes = b"") -> int:
    """How many bytes at the end of ``buf`` may be the start of a private mode sequence
    split across reads. ``before`` is the output that came just before ``buf``."""
    tail = buf[-_HOLD_LIMIT:]
    match = _PRIVATE_MODE_START.search(tail)
    if not match:
        return 0
    start = len(buf) - len(tail) + match.start()
    if buf[start] == 0x9B and not _c1_csi_at(before + buf, len(before) + start):
        return 0  # a UTF-8 character, not the start of a sequence
    return len(match.group(0))


def _alt_modes(params: bytes) -> list[int]:
    """The alternate-screen modes in a private mode parameter list, in order."""
    modes = []
    for param in params.split(b";"):
        digits = param.lstrip(b"0")
        if 0 < len(digits) <= 4 and int(digits) in _ALT_MODES:
            modes.append(int(digits))
    return modes


class _AltScreen:
    """Drops the output that a program draws on the alternate screen from the capture
    (§6.2). Capture stops at a set sequence and resumes after the reset of the SAME mode;
    a reset of another mode, or a second set, changes nothing."""

    def __init__(self) -> None:
        self.mode: int | None = None  # the alternate-screen mode in use, None: main screen
        self._held = b""
        # The last bytes before ``_held``: a UTF-8 lead byte for a ``0x9b`` in the next read.
        self._before = b""

    def filter(self, data: bytes) -> bytes:
        buf = self._held + data
        self._held = b""
        context = self._before
        text = context + buf  # ``buf`` with the bytes before it, for ``_c1_csi_at``
        kept = bytearray()
        pos = 0
        for match in _PRIVATE_MODE.finditer(buf):
            if buf[match.start()] == 0x9B and not _c1_csi_at(text, len(context) + match.start()):
                continue  # part of a UTF-8 character: plain output, kept below
            before = self.mode
            if before is None:
                kept += buf[pos : match.start()]
            action = match.group(2)
            for mode in _alt_modes(match.group(1)):
                if action == b"h" and self.mode is None:
                    self.mode = mode
                elif action == b"l" and self.mode == mode:
                    self.mode = None
            if before is None and self.mode is None:
                kept += match.group(0)  # no switch: plain output on the main screen
            pos = match.end()
        rest = buf[pos:]
        held = _held_tail(rest, text[: len(context) + pos][-3:])
        if held:
            self._held, rest = rest[-held:], rest[:-held]
        self._before = text[: len(text) - held][-3:]
        if self.mode is None:
            kept += rest
        return bytes(kept)

    def flush(self) -> bytes:
        held, self._held = self._held, b""
        return held if self.mode is None else b""


def _open_private(fd: int) -> tuple[int, bool]:
    """A non-blocking way to write to what ``fd`` refers to, without changing ``fd``.

    ``O_NONBLOCK`` belongs to the open file description, which every copy of ``fd``
    shares (on a real terminal: the login shell and its background jobs). So the terminal
    or pipe is opened again, giving nl-shell its own description; it is closed at the end
    of the run. Returns ``(fd, own)``: ``own`` is False when ``fd`` itself is returned,
    for a regular file (writes never block, and a reopen would lose its offset) or when
    nothing could be opened."""
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return fd, False
    if stat.S_ISREG(mode):
        return fd, False
    paths = []
    if os.isatty(fd):
        try:
            paths.append(os.ttyname(fd))
        except OSError:
            pass
    paths.append(f"/proc/self/fd/{fd}")
    flags = os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC
    for path in paths:
        try:
            return os.open(path, flags), True
        except OSError:
            continue
    return fd, False


class Terminal:
    """The user's real terminal: ``input_fd`` (a tty, put in raw mode while a command runs)
    and ``output_fd`` (where PTY output is copied; ``input_fd`` if not given)."""

    def __init__(self, input_fd: int, output_fd: int | None = None) -> None:
        self.input_fd = input_fd
        self.output_fd = input_fd if output_fd is None else output_fd

    def own(self, master_fd: int) -> "_Ownership":
        """One command's hold on this terminal (used by ``Session.run``)."""
        return _Ownership(self, master_fd)


class _Ownership:
    """The terminal side of one ``Session.run``. The session's loop drives it: it calls
    :meth:`begin` before the paste, :meth:`read_fds` / :meth:`write_fds` /
    :meth:`pump` at each turn of its loop, :meth:`stop` at the ``D`` record, passes every
    byte of the command's output through :meth:`mirror` and :meth:`capture`, and calls
    :meth:`end` on every way out."""

    def __init__(self, terminal: Terminal, master_fd: int) -> None:
        self._terminal = terminal
        self._master = master_fd
        self._pending = bytearray()  # read from the real terminal, not yet written
        self._forwarding = False
        self._input_open = True
        self._output_open = True
        self._saved_mode: list | None = None
        self._master_blocking: bool | None = None
        self._out = terminal.output_fd  # where the copy is written (see begin)
        self._out_own = False  # _out is nl-shell's own description, closed in end()
        self._mirror = bytearray()  # PTY output not yet taken by the real terminal
        self.mirror_dropped = False  # some of the copy never reached the real terminal
        self._previous_winch: object = None
        self._winch_installed = False
        self._alt = _AltScreen()
        self.forwarded = False  # a byte reached the PTY: capture stopped (decision 7)

    # ----- the window --

    def begin(self) -> None:
        """Before the paste: raw mode, drop what was typed before, follow window sizes,
        and make writes to the master non-blocking so a full tty input buffer never
        stalls the session's loop. The copy of the output goes through a non-blocking
        description of nl-shell's own, so a stuck terminal never blocks the loop either
        and the flags of the terminal's shared fds are not touched."""
        fd = self._terminal.input_fd
        self._saved_mode = termios.tcgetattr(fd)
        tty.setraw(fd, termios.TCSANOW)
        termios.tcflush(fd, termios.TCIFLUSH)  # typed before the paste: not this command's
        self._master_blocking = os.get_blocking(self._master)
        os.set_blocking(self._master, False)
        self._out, self._out_own = _open_private(self._terminal.output_fd)
        if threading.current_thread() is threading.main_thread():
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
            self._winch_installed = True
        self._copy_size()
        self._forwarding = True

    def stop(self) -> None:
        """The ``D`` record: forwarding ends; input not yet written is dropped."""
        self._forwarding = False
        self._drop_pending()

    def end(self) -> None:
        """Every way out of ``Session.run``: undo :meth:`begin`."""
        self.stop()
        self._alt = _AltScreen()
        try:
            self.settle()
            if self._winch_installed:
                signal.signal(signal.SIGWINCH, self._previous_winch)  # type: ignore[arg-type]
                self._winch_installed = False
        finally:
            try:
                if self._master_blocking is not None:
                    try:
                        os.set_blocking(self._master, self._master_blocking)
                    except OSError:
                        pass
                    self._master_blocking = None
                if self._out_own:
                    self._out_own = False
                    try:
                        os.close(self._out)
                    except OSError:
                        pass
                self._out = self._terminal.output_fd
            finally:
                if self._saved_mode is not None:
                    termios.tcsetattr(
                        self._terminal.input_fd, termios.TCSANOW, self._saved_mode
                    )
                    self._saved_mode = None

    # ----- forwarding (§6.1, §6.4) --

    def read_fds(self) -> list[int]:
        """Read the real terminal only while forwarding and nothing is queued, so input
        never piles up in nl-shell."""
        if self._forwarding and self._input_open and not self._pending:
            return [self._terminal.input_fd]
        return []

    def write_fds(self) -> list[int]:
        fds = [self._master] if self._forwarding and self._pending else []
        if self._mirror and self._output_open:
            fds.append(self._out)
        return fds

    def pump(self, readable, writable, check_control) -> None:
        """Copy queued output to the real terminal if it is ``writable``; read the real
        terminal if it is ``readable``, then write at most one chunk to the master.
        ``check_control()`` handles whatever the control channel holds and returns True
        once forwarding must stop; it runs before every write to the master."""
        if self._out in writable:
            self._write_mirror()
        if not self._forwarding:
            return
        fd = self._terminal.input_fd
        if fd in readable and not self._pending:
            try:
                data = os.read(fd, CHUNK)
            except (BlockingIOError, InterruptedError):
                data = None
            except OSError:
                data = b""
            if data == b"":
                self._input_open = False  # the real terminal went away
            elif data:
                self._pending += data
            data = None
        if not self._pending or self._master not in writable:
            return
        if check_control() or not self._forwarding:
            self.stop()
            return
        try:
            written = os.write(self._master, self._pending[:CHUNK])
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.stop()  # the session's PTY is gone; the session loop reports it
            return
        if written:
            self.forwarded = True
            del self._pending[:written]

    def _drop_pending(self) -> None:
        for i in range(len(self._pending)):
            self._pending[i] = 0
        del self._pending[:]

    # ----- output (§6.2) --

    def mirror(self, data: bytes) -> None:
        """Copy the command's output to the real terminal: queue it and write what the
        terminal takes now, without waiting. :meth:`pump` writes the rest when the
        terminal is writable. Past ``MIRROR_LIMIT`` queued bytes the copy is dropped."""
        if not self._output_open:
            return
        room = MIRROR_LIMIT - len(self._mirror)
        if len(data) > room:
            self.mirror_dropped = True
            data = data[: max(room, 0)]
        self._mirror += data
        self._write_mirror()

    def _write_mirror(self) -> bool:
        """One write of the queued copy that cannot block; True if the terminal took
        bytes."""
        if not self._mirror or not self._output_open:
            return False
        data = self._mirror
        if not self._out_own:
            # The shared fd keeps its flags (it may block): write only what fits now.
            try:
                if not select.select([], [self._out], [], 0)[1]:
                    return False
            except (OSError, ValueError):
                return False
            data = self._mirror[:_SHARED_CHUNK]
        try:
            written = os.write(self._out, data)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            self._output_open = False  # the real terminal went away
            self._drop_mirror()
            return False
        del self._mirror[:written]
        return written > 0

    def settle(self) -> None:
        """At the end of the run: write the rest of the copy while the terminal keeps
        taking it, for at most ``MIRROR_SETTLE`` s; then drop what is left and set
        ``mirror_dropped``. ``Session`` calls it before building the result."""
        fd = self._out
        deadline = time.monotonic() + MIRROR_SETTLE
        try:
            while self._mirror and self._output_open:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    _, ready, _ = select.select([], [fd], [], min(left, _MIRROR_STALL))
                except (OSError, ValueError):
                    break
                if not ready:
                    break  # the terminal stopped reading
                self._write_mirror()
        finally:
            if self._mirror:
                self.mirror_dropped = True
                self._drop_mirror()

    def _drop_mirror(self) -> None:
        # The copy may hold the echo of forwarded input: zeroed, like ``_pending``.
        for i in range(len(self._mirror)):
            self._mirror[i] = 0
        del self._mirror[:]

    def capture(self, data: bytes) -> bytes:
        """What of ``data`` the result may keep: nothing on the alternate screen, and
        nothing once a byte has been forwarded."""
        kept = self._alt.filter(data)
        return b"" if self.forwarded else kept

    def flush(self) -> bytes:
        """At the end of the output: the bytes held back in case they started a sequence."""
        kept = self._alt.flush()
        return b"" if self.forwarded else kept

    # ----- window size (§6.3) --

    def _on_winch(self, signum, frame) -> None:
        self._copy_size()

    def _copy_size(self) -> None:
        try:
            size = fcntl.ioctl(self._terminal.input_fd, termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols = struct.unpack("HHHH", size)[:2]
            if rows and cols:
                fcntl.ioctl(self._master, termios.TIOCSWINSZ, size)
        except OSError:
            pass  # not a terminal, or gone: the PTY keeps its size
