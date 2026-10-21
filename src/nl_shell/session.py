"""The persistent bash session: one interactive bash on a PTY, driven by pty-protocol v1.

``Session.start()`` runs ``bash --noprofile --rcfile <rcfile> -i`` on a PTY (ptyprocess,
ADR-0021 §2) with a pipe as fd 3, the control channel (spec
``docs/design/specs/pty-protocol/v1.md`` §1, ADR-0014). ``Session.run()`` pastes one
confirmed command (§5) and returns its status, ``PIPESTATUS``, output and snapshot. Status
and completion come only from control-channel records, never from text on the PTY.

A protocol violation puts the session in degraded mode (§4.2) until ``restart()``; while
degraded nothing more is written to the PTY (§4.3). Terminal ownership (§6) and the REPL
are later work (#102, #78). The prompt rule reads Linux ``/proc``.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import platform
import secrets
import select
import shutil
import stat
import struct
import tempfile
import time
from typing import Callable

import ptyprocess

from nl_shell.pty_protocol import (
    PASTE_END,
    PASTE_START,
    CommandRefused,
    Done,
    Phase,
    ProtocolTracker,
    Snapshot,
    check_command,
)

__all__ = [
    "CommandRefused",
    "CommandResult",
    "HOOK_STATE_VARS",
    "RCFILE",
    "Session",
    "SessionDegraded",
    "SessionError",
    "SessionTimeout",
    "Snapshot",
]

# Every shell name the rcfile's hooks use, variables and functions (no `local`: a readonly
# global of the same name would make it fail). H carries `declare -p` of each (value and
# attributes), so a confirmed change to one degrades at that command's own snapshot. The
# hooks overwrite their scratch variables, so `__nls_done` first saves their `declare -p`
# text in `__nls_pre` (which is in H), and `__nls_snap` resets them before E. The counter
# `__nls_seq` must equal the D record's number (pty_protocol.expected_hooks).
HOOK_STATE_VARS = (
    "__nls_c",
    "__nls_done",
    "__nls_ea",
    "__nls_enc",
    "__nls_n",
    "__nls_pre",
    "__nls_ps",
    "__nls_ret",
    "__nls_seq",
    "__nls_snap",
    "__nls_st",
    "__nls_user_pc",
    "__nls_v",
)

# The pager and loader variables of policy-gate v1 §7.5, reported in V.
_V_VARS = (
    "PAGER MANPAGER SYSTEMD_PAGER GIT_PAGER LESSOPEN LESSCLOSE LD_PRELOAD LD_LIBRARY_PATH"
    " BASH_ENV ENV"
)

# The rcfile: the spec's reference (§1.3, which explains it; H and V are §3). In short:
# - PS0 writes S for every line bash reads, with no guard variable. nl-shell's hook is the
#   last PROMPT_COMMAND element (element 0 is empty, so `PROMPT_COMMAND=:` leaves the hook
#   in place and changes H). `__nls_done` saves the status and the scratch variables
#   (`__nls_pre`), prints pending job notices (`jobs -n`: bash 5.2 would print them after
#   the marker), prints the end-of-output marker, writes D and runs the user's
#   PROMPT_COMMAND elements; then `__nls_snap` sets PS1/PS2 (plain or exported ones only;
#   others show in H), writes the snapshot and E, and sets PS1/PS2 again. Output is what lies
#   between readline's end of the paste and that marker.
# - only builtins and parameter expansion (no `PATH` lookup). Each `$( … )` is parsed
#   again when it runs, so `__nls_snap` turns `expand_aliases` off first (back on after E
#   if it was on) and PS0/PROMPT_COMMAND quote their command words (`\builtin`).
# - what this protects is the closed list in spec §1.3; nothing else is claimed yet
#   (#141, #145).
RCFILE = (
    r"""# nl-shell session rcfile (pty-protocol v1 §1.3). Written by nl-shell; do not edit.
[ -f ~/.bashrc ] && . ~/.bashrc
set +H
unset HISTFILE
bind 'set enable-bracketed-paste on' 2>/dev/null
shopt -s promptvars
__nls_ea=$(shopt -p expand_aliases); shopt -u expand_aliases
__nls_user_pc=()
if declare -p PROMPT_COMMAND >/dev/null 2>&1; then __nls_user_pc=("${PROMPT_COMMAND[@]}"); fi
__nls_seq=-1 __nls_st=0 __nls_ps=() __nls_v= __nls_c= __nls_n=0 __nls_pre=
PS0='$(\\builtin printf "S %s\\n" "$((__nls_seq + 1))" >&3)'
__nls_ret() { \builtin return "$1"; }
__nls_enc() {
  __nls_v=$2
  __nls_v=${__nls_v//'\'/'\\'}
  __nls_v=${__nls_v//$'\n'/'\n'}
  __nls_v=${__nls_v//$'\r'/'\r'}
  \builtin printf '%s %s\n' "$1" "$__nls_v" >&3
}
__nls_done() {
  \builtin set -- "$?" "${PIPESTATUS[@]}"
  __nls_pre=$(\builtin declare -p __nls_pre __nls_c __nls_n __nls_ps __nls_st __nls_v 2>&1) ||
    \builtin :
  __nls_st=$1
  \builtin shift
  __nls_ps=("$@")
  \builtin jobs -n
  \builtin printf '\033]7700;@NLS_END@\007'
  __nls_seq=$((__nls_seq + 1))
  \builtin printf -v __nls_v ' %s' "${__nls_ps[@]}"
  \builtin printf 'D %s %s%s\n' "$__nls_seq" "$__nls_st" "$__nls_v" >&3
  for __nls_c in "${__nls_user_pc[@]}"; do
    if __nls_ret "$__nls_st"; then
      \builtin eval "$__nls_c" || \builtin :
    else
      \builtin eval "$__nls_c" || \builtin :
    fi
  done
  __nls_st=0 __nls_ps=() __nls_v= __nls_c= __nls_n=0
}
__nls_snap() {
  \builtin set -- "$1" "${BASHOPTS-}"
  \builtin shopt -u expand_aliases
  for __nls_c in PS1 PS2; do
    __nls_v=$(\builtin declare -p "$__nls_c" 2>/dev/null) || __nls_v=
    if [[ -z $__nls_v || ${__nls_v%%=*} == "declare -"[-x]" $__nls_c" ]]; then
      if [[ $__nls_c == PS1 ]]; then PS1='\$ '; else PS2='> '; fi
    fi
  done
  __nls_v= __nls_c=
  __nls_enc H "$( {
    \builtin declare -p PS0 PROMPT_COMMAND __nls_user_pc TERM PS1 PS2
    \builtin shopt -p promptvars
    for __nls_c in """
    + " ".join(HOOK_STATE_VARS)
    + r"""; do
      \builtin declare -p "$__nls_c" 2>/dev/null || \builtin printf '%s unset\n' "$__nls_c"
    done
    \builtin declare -f __nls_done __nls_snap __nls_enc __nls_ret
  } 2>&1 )"
  __nls_enc V "$(
    for __nls_c in """
    + _V_VARS
    + r"""; do
      \builtin declare -p "$__nls_c" 2>/dev/null || \builtin printf '%s unset\n' "$__nls_c"
    done
  )"
  __nls_enc F "$(\builtin compgen -A function)"
  __nls_enc L "$(\builtin alias -p)"
  __nls_enc T "$1"
  __nls_enc B "$(\builtin enable -n)"
  __nls_enc X "$(\builtin shopt -p extdebug)"
  __nls_enc P "${PATH-}"
  __nls_v=$(\builtin jobs -ps) __nls_n=0
  if [[ -n $__nls_v ]]; then __nls_c=${__nls_v//[!$'\n']/}; __nls_n=$((${#__nls_c} + 1)); fi
  \builtin printf 'J %s\n' "$__nls_n" >&3
  __nls_st=0 __nls_ps=() __nls_v= __nls_c= __nls_n=0 __nls_pre=
  \builtin printf 'E\n' >&3
  PS1='\$ ' PS2='> '
  if [[ :$2: == *:expand_aliases:* ]]; then \builtin shopt -s expand_aliases; fi
}
\builtin unset PROMPT_COMMAND
PROMPT_COMMAND=('' '\__nls_done; \__nls_snap "$(\builtin trap -p)"')
builtin eval "$__nls_ea"; builtin unset __nls_ea
"""
)

# Readline writes this when it hands a finished line to bash (bracketed paste mode off),
# just before PS0 runs. Before it is readline's echo of the paste.
_LINE_ACCEPTED = b"\x1b[?2004l"

# Where the rcfile prints the end-of-output marker's token; `_spawn` puts a random one in.
_END_TOKEN = "@NLS_END@"


def _end_marker(token: str) -> bytes:
    """The OSC sequence `__nls_done` prints before `D` and the user's hook; random per start."""
    return b"\x1b]7700;" + token.encode("ascii") + b"\x07"


# Captured output kept per command; past this only the tail is kept and
# ``output_truncated`` is set.
MAX_OUTPUT = 1 << 20

# §4.2 third trigger: how long bash must be blocked reading the terminal, with no D (or
# E), before the session degrades.
PROMPT_GRACE = 1.0

# Bound on waiting for the prompt before a paste, or for the end marker after E.
_SETTLE = 3.0

# System call numbers for reading `/proc/<pid>/syscall`. readline waits for a line in
# `pselect6` with nfds 1 (fd 0 only); the `read` builtin reads fd 0 with `read`.
_SYSCALLS = {
    "x86_64": {"read": 0, "select": 23, "pselect6": 270},
    "aarch64": {"read": 63, "pselect6": 72},
}

# Linux `TIOCGPTN` (the PTY number of a master), for Pythons without `os.ptsname`.
_TIOCGPTN = 0x80045430

_POLL = 0.02

_DEV_TTY = os.makedev(5, 0)


class SessionError(Exception):
    """The session cannot do what was asked."""


class SessionDegraded(SessionError):
    """The session is in degraded mode (§4.2); ``reason`` says why. ``result`` is the
    command's result when its own snapshot degraded the session (``H`` changed)."""

    def __init__(self, reason: str, result: "CommandResult | None" = None):
        super().__init__(f"session degraded: {reason}")
        self.reason = reason
        self.result = result


class SessionTimeout(SessionError, TimeoutError):
    """No done record within the caller's timeout (the session degrades)."""


class _ProcUnreadable(Exception):
    """``/proc/<pid>/syscall`` could not be read: the prompt rule cannot be applied."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """One command, as the control channel reported it."""

    seq: int
    ran: bool  # False: `D` came without `S`, bash read the input but ran nothing (§4.1)
    status: int  # `$?` from the D record
    pipestatus: tuple[int, ...]
    output: bytes  # PTY output between S and D, the tail when truncated
    output_truncated: bool
    snapshot: Snapshot
    variables_changed: bool  # the snapshot's V differs from the baseline's

    @property
    def text(self) -> str:
        """``output`` decoded, with the PTY's CR LF turned back into LF."""
        return self.output.decode("utf-8", "replace").replace("\r\n", "\n")


class _Output:
    """The command's share of the PTY stream, bounded: after readline's end of the paste,
    before the end marker. Later bytes are dropped. Markers may be split across reads."""

    def __init__(self, limit: int, end: bytes) -> None:
        self.limit = limit
        self.end = end
        self.data = bytearray()
        self.truncated = False
        self._accepted = False
        self._ended = False
        self._skip_cr = False
        self._pending = b""  # bytes that may be the start of a marker split across reads

    @property
    def ended(self) -> bool:
        """The end marker was seen: nothing more is the command's."""
        return self._ended

    @property
    def delimited(self) -> bool:
        """Both markers were seen, in order, so ``data`` is exactly the command's output."""
        return self._accepted and self._ended

    def feed(self, chunk: bytes) -> None:
        if self._ended:
            return
        buf = self._pending + chunk
        self._pending = b""
        if not self._accepted:
            pos = buf.find(_LINE_ACCEPTED)
            end = buf.find(self.end)
            if end >= 0 and (pos < 0 or end < pos):
                self._ended = True  # the command ended before readline ended the paste
                return
            if pos < 0:
                self._pending = buf[-(max(len(_LINE_ACCEPTED), len(self.end)) - 1) :]
                return
            self._accepted = True
            self._skip_cr = True
            buf = buf[pos + len(_LINE_ACCEPTED) :]
        if self._skip_cr and buf:
            # readline moves to column 0 after the accepted line; that CR isn't output.
            self._skip_cr = False
            if buf[:1] == b"\r":
                buf = buf[1:]
        pos = buf.find(self.end)
        if pos >= 0:
            self._ended = True
            self._append(buf[:pos])
            return
        keep = min(len(buf), len(self.end) - 1)
        self._append(buf[: len(buf) - keep])
        self._pending = buf[len(buf) - keep :]

    def _append(self, data: bytes) -> None:
        self.data += data
        excess = len(self.data) - self.limit
        if excess > 0:
            del self.data[:excess]
            self.truncated = True


class Session:
    """One long-lived interactive bash on a PTY with nl-shell's control channel on fd 3."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        dimensions: tuple[int, int] = (24, 80),
        on_record: Callable[[bytes], None] | None = None,
        max_output: int = MAX_OUTPUT,
        proc_root: str = "/proc",
    ):
        """Prefer :meth:`start`. ``on_record`` sees every control record.

        ``proc_root`` is where bash's ``<pid>/syscall`` is read for the prompt rule (§4.2);
        if it cannot be read the session degrades with a reason naming the file.
        """
        if max_output < 1:
            raise ValueError("max_output must be at least 1")
        self.max_output = max_output
        self.proc_root = proc_root
        self._env = dict(os.environ if env is None else env)
        if self._env.get("TERM", "dumb") == "dumb":
            # Readline turns bracketed paste off on a dumb terminal (§5.1 needs it).
            self._env["TERM"] = "xterm-256color"
        self._cwd = cwd
        self._dimensions = dimensions
        self._on_record = on_record
        self._proc: ptyprocess.PtyProcess | None = None
        self._ctl: int | None = None
        self._ctl_open = False
        self._rcdir: str | None = None
        self._tracker = ProtocolTracker(on_record)
        self._end = b""  # this start's end-of-output marker (_end_marker)
        self._tty_rdev: int | None = None  # device number of the PTY slave (bash's fd 0)

    # ----- lifecycle --

    @classmethod
    def start(cls, *, timeout: float = 10.0, **kwargs) -> "Session":
        """Start bash and wait for the baseline snapshot (§1, §3). Raises if that fails; a
        session that cannot apply the prompt rule is returned degraded."""
        session = cls(**kwargs)
        try:
            session._spawn(timeout)
        except BaseException:
            session.close()
            raise
        return session

    def restart(self, timeout: float = 10.0) -> None:
        """Fresh bash, fresh baseline, not degraded. Restoring the cwd and logging the
        restart (spec §4.4-§4.5) land with #121."""
        self.close()
        self._tracker = ProtocolTracker(self._on_record)
        try:
            self._spawn(timeout)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """End bash (and its jobs, via SIGHUP) and release every descriptor."""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.close(force=True)
            except Exception:
                pass
        if self._ctl is not None:
            os.close(self._ctl)
            self._ctl = None
        self._ctl_open = False
        self._remove_rcfile()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _spawn(self, timeout: float) -> None:
        self._rcdir = tempfile.mkdtemp(prefix="nl-shell-session-")
        rcfile = os.path.join(self._rcdir, "rc.bash")
        token = secrets.token_hex(16)
        self._end = _end_marker(token)
        with open(rcfile, "w", encoding="utf-8") as fh:
            fh.write(RCFILE.replace(_END_TOKEN, token))
        ctl_read, ctl_write = os.pipe()

        def wire_fd3() -> None:  # runs in the child, after fork
            high = fcntl.fcntl(ctl_write, fcntl.F_DUPFD, 10)
            os.dup2(high, 3)  # a dup2 copy is inheritable
            for fd in {high, ctl_write} - {3}:
                os.close(fd)

        try:
            self._proc = ptyprocess.PtyProcess.spawn(
                ["bash", "--noprofile", "--rcfile", rcfile, "-i"],
                cwd=self._cwd,
                env=self._env,
                dimensions=self._dimensions,
                pass_fds=(ctl_write,),
                preexec_fn=wire_fd3,
            )
        except BaseException:
            os.close(ctl_read)
            raise
        finally:
            os.close(ctl_write)  # only bash (and what it starts) holds the write end
        self._ctl, self._ctl_open = ctl_read, True
        self._tty_rdev = _pty_slave_rdev(self._proc.fd)

        deadline = time.monotonic() + timeout
        while self._tracker.phase in (Phase.BASELINE, Phase.SNAPSHOT):
            if time.monotonic() > deadline:
                raise SessionTimeout("bash did not report its first snapshot in time")
            ready = _select([self._proc.fd, self._ctl], _POLL)
            if self._proc.fd in ready:
                self._read_pty()  # start-up output and the first prompt: not kept
            if self._ctl in ready:
                self._read_control()
            if self._tracker.degraded:
                raise SessionDegraded(self._tracker.degraded_reason or "unknown")
        self._remove_rcfile()  # bash has read it
        # Fail closed at start if the prompt rule cannot be applied (§4.2 third trigger).
        self._wait_for_prompt()

    def _remove_rcfile(self) -> None:
        if self._rcdir is not None:
            shutil.rmtree(self._rcdir, ignore_errors=True)
            self._rcdir = None

    # ----- state --

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def degraded(self) -> bool:
        return self._tracker.degraded

    @property
    def degraded_reason(self) -> str | None:
        return self._tracker.degraded_reason

    @property
    def baseline(self) -> Snapshot | None:
        return self._tracker.baseline

    @property
    def snapshot(self) -> Snapshot | None:
        return self._tracker.snapshot

    # ----- running a command --

    def interrupt(self) -> bool:
        """Write Ctrl-C (``\\x03``) to the PTY, only while a command runs (after its
        ``S``, before its ``D``); else return ``False``. At a prompt it would make bash
        send an unrequested ``D``. One that races the command's end degrades the session.
        """
        if self._proc is None:
            raise SessionError("the session is closed")
        if self._tracker.phase is not Phase.RUNNING:
            return False
        _write_all(self._proc.fd, b"\x03")
        return True

    def run(self, command: str | bytes, *, timeout: float | None = None) -> CommandResult:
        """Paste one confirmed command (§5) and wait for its done record and snapshot.

        Raises :class:`CommandRefused` or :class:`SessionDegraded` (nothing written unless
        the command's own run degraded the session), or :class:`SessionTimeout`.
        """
        data = check_command(command)
        if self._proc is None:
            raise SessionError("the session is closed")
        self._raise_if_degraded()

        self._drain_pty()  # the last prompt and any job notices: not this command's
        # A record that arrived after the last E is out of order; the session must
        # degrade before bash gets the next command (§4.2).
        self._drain_control()
        self._raise_if_degraded()
        # Paste only into readline: bash must be waiting for a line.
        self._wait_for_prompt()
        self._raise_if_degraded()
        self._tracker.command_sent()
        _write_all(self._proc.fd, PASTE_START + data + PASTE_END + b"\r")
        return self._wait_for_result(timeout)

    def _raise_if_degraded(self) -> None:
        if self._tracker.degraded:
            raise SessionDegraded(self._tracker.degraded_reason or "unknown")

    def _wait_for_result(self, timeout: float | None) -> CommandResult:
        assert self._proc is not None and self._ctl is not None
        fd = self._proc.fd
        deadline = None if timeout is None else time.monotonic() + timeout
        output = _Output(self.max_output, self._end)
        done: Done | None = None
        finished: Snapshot | None = None
        waiting_since: float | None = None  # bash has been reading the terminal since

        while True:
            watch = [fd, self._ctl] if self._ctl_open else [fd]
            ready = _select(watch, _POLL)
            if fd in ready:
                output.feed(self._read_pty())
            if self._ctl_open and self._ctl in ready:
                for kind, value in self._read_control():
                    if kind == "D" and done is None:
                        done = value  # type: ignore[assignment]
                    elif kind == "E":
                        finished = value  # type: ignore[assignment]
                        if self._tracker.started and not self._tracker.degraded:
                            self._await_output_end(output)
                if finished is not None:
                    self._drain_control()  # a record after E degrades, checked below

            if self._tracker.degraded:
                result = None
                snapshot = self._tracker.snapshot
                if done is not None and snapshot is not None and snapshot.seq == done.seq:
                    result = self._result(done, snapshot, output)
                raise SessionDegraded(self._tracker.degraded_reason or "unknown", result)
            if finished is not None:
                assert done is not None
                return self._result(done, finished, output)

            now = time.monotonic()
            # §4.2 third trigger; until E, as a snapshot cut short also ends at a prompt.
            try:
                reading = self._reading_terminal()
            except _ProcUnreadable as exc:
                self._tracker.degrade(str(exc))
                continue
            if reading:
                if waiting_since is None:
                    waiting_since = now
                elif now - waiting_since >= PROMPT_GRACE:
                    self._tracker.degrade(
                        "no done record after the shell returned to a prompt"
                        if done is None
                        else "the snapshot did not finish after the shell returned to a prompt"
                    )
                    continue
            else:
                waiting_since = None
            if deadline is not None and now > deadline:
                self._tracker.degrade("no done record within the caller's timeout")
                raise SessionTimeout(f"command did not finish within {timeout} s")

    def _await_output_end(self, output: _Output) -> None:
        """After ``E``: read the PTY up to the end marker; degrade if the output can't be
        delimited (reporting partial output for a command that ran would lie)."""
        assert self._proc is not None
        deadline = time.monotonic() + _SETTLE
        while not output.ended and not self._tracker.degraded:
            if time.monotonic() > deadline:
                break
            if _select([self._proc.fd], _POLL):
                output.feed(self._read_pty())
        if self._tracker.degraded:
            return
        if not output.delimited:
            self._tracker.degrade(
                "the command's output could not be delimited (readline did not end a"
                " bracketed paste, or the end marker before D never reached the terminal)"
            )

    def _result(self, done: Done, snapshot: Snapshot, output: _Output) -> CommandResult:
        baseline = self._tracker.baseline
        assert baseline is not None
        return CommandResult(
            seq=done.seq,
            ran=self._tracker.started,
            status=done.status,
            pipestatus=done.pipestatus,
            output=bytes(output.data) if self._tracker.started else b"",
            output_truncated=output.truncated and self._tracker.started,
            snapshot=snapshot,
            variables_changed=snapshot.variables != baseline.variables,
        )

    # ----- the prompt rule (§4.2, brief decision 7) --

    def _wait_for_prompt(self) -> bool:
        """Wait until readline waits for a line; degrade (and return False) if it doesn't."""
        deadline = time.monotonic() + _SETTLE
        while True:
            try:
                if self._reading_terminal():
                    return True
            except _ProcUnreadable as exc:
                self._tracker.degrade(str(exc))
                return False
            if time.monotonic() > deadline:
                self._tracker.degrade("the shell did not return to its prompt")
                return False
            self._drain_control()
            if self._tracker.degraded:
                return False
            time.sleep(0.002)

    def _syscall_path(self) -> str:
        assert self._proc is not None
        return os.path.join(self.proc_root, str(self._proc.pid), "syscall")

    def _syscall(self) -> tuple[int, int] | None:
        """bash's blocking system call and first argument, None while it runs; raises
        :class:`_ProcUnreadable`."""
        path = self._syscall_path()
        try:
            with open(path, encoding="ascii") as fh:
                fields = fh.read().split()
        except (OSError, UnicodeDecodeError) as exc:
            raise _ProcUnreadable(
                f"cannot read {path} ({exc.__class__.__name__}), so the prompt rule"
                " (no done record after the shell returned to a prompt) cannot be applied"
            ) from exc
        if fields[:1] == ["running"]:
            return None
        try:
            return int(fields[0]), int(fields[1], 16)
        except (ValueError, IndexError) as exc:
            raise _ProcUnreadable(f"cannot parse {path}: {' '.join(fields[:2])!r}") from exc

    @staticmethod
    def _nr(name: str) -> int:
        table = _SYSCALLS.get(platform.machine(), {})
        return table.get(name, -2)

    def _reading_terminal(self) -> bool:
        """bash is blocked in ``read(0, …)`` or ``pselect6``/``select`` with nfds 1 (as
        readline waits), and fd 0 is this PTY, not a redirect (``read x < <(…)``)."""
        call = self._syscall()
        if call is None:
            return False
        nr, arg0 = call
        if nr == self._nr("read"):
            blocked = arg0 == 0
        else:
            blocked = nr in (self._nr("pselect6"), self._nr("select")) and arg0 == 1
        return blocked and self._stdin_is_terminal()

    def _stdin_is_terminal(self) -> bool:
        """bash's fd 0 is this PTY (its slave or /dev/tty); raises
        :class:`_ProcUnreadable` if unknown."""
        assert self._proc is not None
        path = os.path.join(self.proc_root, str(self._proc.pid), "fd", "0")
        if self._tty_rdev is None:
            raise _ProcUnreadable(
                "cannot identify the session's terminal, so the prompt rule"
                " (no done record after the shell returned to a prompt) cannot be applied"
            )
        try:
            info = os.stat(path)
        except OSError as exc:
            raise _ProcUnreadable(
                f"cannot read {path} ({exc.__class__.__name__}), so the prompt rule"
                " (no done record after the shell returned to a prompt) cannot be applied"
            ) from exc
        # `/dev/tty` (5, 0) is bash's controlling terminal, which is this PTY too.
        return stat.S_ISCHR(info.st_mode) and info.st_rdev in (self._tty_rdev, _DEV_TTY)

    # ----- helpers --

    def _read_pty(self) -> bytes:
        assert self._proc is not None
        try:
            chunk = os.read(self._proc.fd, 65536)
        except OSError:
            chunk = b""  # EIO: bash has exited and the slave side is gone
        if not chunk:
            self._tracker.degrade("the shell exited")
        return chunk

    def _drain_pty(self) -> None:
        """Read (and drop) what the PTY holds right now, without waiting for more."""
        assert self._proc is not None
        while not self._tracker.degraded and _select([self._proc.fd], 0):
            if not self._read_pty():
                break

    def _drain_control(self) -> None:
        """Feed the tracker whatever the control channel holds right now."""
        while self._ctl_open and not self._tracker.degraded and _select([self._ctl], 0):
            self._read_control()

    def _read_control(self) -> list[tuple[str, object]]:
        assert self._ctl is not None
        data = os.read(self._ctl, 65536)
        if not data:
            self._ctl_open = False
            self._tracker.eof()
            return []
        return self._tracker.feed(data)


def _pty_slave_rdev(master_fd: int) -> int | None:
    """The device number of the PTY slave paired with ``master_fd``, or None if unknown."""
    try:
        if hasattr(os, "ptsname"):  # Python 3.13+
            name = os.ptsname(master_fd)
        else:
            raw = fcntl.ioctl(master_fd, _TIOCGPTN, b"\0\0\0\0")
            name = f"/dev/pts/{struct.unpack('I', raw)[0]}"
        return os.stat(name).st_rdev
    except OSError:
        return None


def _select(fds: list[int], timeout: float) -> list[int]:
    ready, _, _ = select.select(fds, [], [], timeout)
    return ready


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], _POLL)
            continue
        view = view[written:]
