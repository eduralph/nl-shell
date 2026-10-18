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
import re
import secrets
import select
import shutil
import stat
import struct
import tempfile
import termios
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
    "HOOK_BUILTINS",
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

# Every builtin the hooks call: exactly the names written `\builtin <name>` in the hook
# functions, PS0 and nl-shell's PROMPT_COMMAND element, plus `builtin` itself (the rcfile
# lists them quoted, so the list never reads as a call). H lists which of them are disabled, so
# `enable -n` of one degrades at that command's snapshot; other disabled builtins are only
# reported in B. Disabling `printf`, `builtin` or `set` stops the hooks writing records at
# all; the prompt rule then degrades the session ("no done record").
HOOK_BUILTINS = (
    ":",
    "alias",
    "bind",
    "compgen",
    "declare",
    "enable",
    "eval",
    "jobs",
    "printf",
    "return",
    "set",
    "shift",
    "shopt",
    "trap",
    "builtin",
)

# The readline keymaps whose bindings H lists (`emacs` and `vi` are other names for
# `emacs-standard` and `vi-command`).
_KEYMAPS = "emacs-standard emacs-meta emacs-ctlx vi-command vi-insert"

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
# - H also holds the bash state that can make bash run code, or change how readline reads
#   the pasted bytes, after the snapshot: every trap but EXIT (read at the top level of
#   nl-shell's PROMPT_COMMAND element, because bash hides DEBUG/RETURN/ERR inside an
#   untraced function, and passed in; signal traps can run while readline waits, and a
#   CHLD trap each time a `$( … )` of the hooks ends; the EXIT trap runs only when bash
#   exits, so it is cut off the front of that list), extdebug, functrace, errtrace (a session whose
#   ~/.bashrc turns functrace on starts degraded: the substitution that reads the traps
#   would inherit a DEBUG trap, which could hide itself there), xtrace, BASH_XTRACEFD, PS4
#   (made readonly after ~/.bashrc, as bash traces PROMPT_COMMAND itself), MAILCHECK
#   (unset and made readonly after ~/.bashrc, so bash never checks mail: the mail check runs
#   after PROMPT_COMMAND, before readline, and expands MAILPATH messages; if ~/.bashrc
#   already made either readonly, the rcfile defines `nl_shell_unpinned_<name>` and the session
#   starts degraded), the disabled hook builtins, readline on/off and its mode, its
#   variables except history-size, the key bindings of the five keymaps, and history
#   expansion. Every `$( … )` in the hooks keeps stderr, so a failing builtin never yields a
#   baseline-matching empty value. If a hook function's first `\builtin set` fails (`set`
#   or `builtin` disabled) it returns, so no record is written and the prompt rule fires
#   (a plain `return`: with `builtin` disabled `\builtin return` would fail too, and every
#   later `\builtin` would run bash's command-not-found handler).
# - the terminal settings are not bash state: nl-shell reads them from outside (spec §3).
# - what this protects is the closed list in spec §1 and §3; nothing else is claimed (#145).
RCFILE = (
    r"""# nl-shell session rcfile (pty-protocol v1 §1.3). Written by nl-shell; do not edit.
[ -f ~/.bashrc ] && . ~/.bashrc
set +H
unset HISTFILE
for __nls_c in PS4 MAILCHECK; do
  __nls_v=$(declare -p "$__nls_c" 2>/dev/null); __nls_v=${__nls_v#declare -}
  if [[ ${__nls_v%% *} == *r* ]]; then eval "nl_shell_unpinned_$__nls_c() { :; }"; fi
done
readonly PS4
unset MAILCHECK 2>/dev/null; readonly MAILCHECK
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
  \builtin set -- "$?" "${PIPESTATUS[@]}" || return
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
  \builtin set -- "$1" "${BASHOPTS-}" || return
  \builtin shopt -u expand_aliases
  for __nls_c in PS1 PS2; do
    __nls_v=$(\builtin declare -p "$__nls_c" 2>&1) || __nls_v=
    if [[ -z $__nls_v || ${__nls_v%%=*} == "declare -"[-x]" $__nls_c" ]]; then
      if [[ $__nls_c == PS1 ]]; then PS1='\$ '; else PS2='> '; fi
    fi
  done
  __nls_v=$(\builtin trap -p EXIT 2>&1) __nls_c=
  __nls_enc H "$( {
    [[ $__nls_v == "trap -- "* ]] || __nls_v=
    __nls_c=${1#"$__nls_v"}
    \builtin printf 'traps but EXIT:\n%s\n' "${__nls_c#$'\n'}"
    __nls_v= __nls_c=
    \builtin declare -p PS0 PROMPT_COMMAND __nls_user_pc TERM PS1 PS2 PS4 MAILCHECK
    \builtin shopt -p promptvars
    \builtin shopt -p extdebug || \builtin :
    \builtin shopt -po emacs vi functrace errtrace xtrace histexpand || \builtin :
    for __nls_c in BASH_XTRACEFD """
    + " ".join(HOOK_STATE_VARS)
    + r"""; do
      \builtin declare -p "$__nls_c" 2>&1 || \builtin printf '%s unset\n' "$__nls_c"
    done
    \builtin declare -f __nls_done __nls_snap __nls_enc __nls_ret
    \builtin declare -pF __nls_done __nls_snap __nls_enc __nls_ret
    for __nls_c in """
    + " ".join(f"'{name}'" for name in HOOK_BUILTINS)
    + r"""; do
      \builtin compgen -A disabled -X "!$__nls_c" -- "$__nls_c" || \builtin :
    done
    __nls_v=$(\builtin bind -v 2>&1)
    __nls_c=${__nls_v#*$'\n'set history-size }
    __nls_v=${__nls_v/$'\n'"set history-size ${__nls_c%%$'\n'*}"$'\n'/$'\n'}
    \builtin printf '%s\n' "$__nls_v"
    for __nls_c in """
    + _KEYMAPS
    + r"""; do
      \builtin bind -m "$__nls_c" -p
      \builtin bind -m "$__nls_c" -s
      \builtin bind -m "$__nls_c" -X
    done
  } 2>&1 )"
  __nls_enc V "$(
    for __nls_c in """
    + _V_VARS
    + r"""; do
      \builtin declare -p "$__nls_c" 2>&1 || \builtin printf '%s unset\n' "$__nls_c"
    done 2>&1
  )"
  __nls_enc F "$(\builtin compgen -A function 2>&1)"
  __nls_enc L "$(\builtin alias -p 2>&1)"
  __nls_enc T "$1"
  __nls_enc B "$(\builtin enable -n 2>&1)"
  __nls_enc X "$(\builtin shopt -p extdebug 2>&1)"
  __nls_enc P "${PATH-}"
  __nls_v=$(\builtin jobs -ps 2>&1) __nls_n=0
  if [[ -n $__nls_v ]]; then __nls_c=${__nls_v//[!$'\n']/}; __nls_n=$((${#__nls_c} + 1)); fi
  \builtin printf 'J %s\n' "$__nls_n" >&3
  __nls_st=0 __nls_ps=() __nls_v= __nls_c= __nls_n=0 __nls_pre=
  \builtin printf 'E\n' >&3
  PS1='\$ ' PS2='> '
  if [[ :$2: == *:expand_aliases:* ]]; then \builtin shopt -s expand_aliases; fi
}
\builtin unset PROMPT_COMMAND
PROMPT_COMMAND=('' '\__nls_done; \__nls_snap "$(\builtin trap -p 2>&1)"')
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

# Where the kernel says whether TIOCSTI (pushing bytes into a terminal's input) is allowed.
TIOCSTI_PATH = "/proc/sys/dev/tty/legacy_tiocsti"

# The H line that shows functrace on.
_FUNCTRACE_ON = "set -o functrace"

# The rcfile defines `nl_shell_unpinned_<name>` when ~/.bashrc already made PS4 or MAILCHECK
# readonly, so nl-shell could not pin it.
_UNPINNED = "nl_shell_unpinned_"

_TERMIOS_FIELDS = ("iflag", "oflag", "cflag", "lflag", "ispeed", "ospeed", "cc")

# R, the terminal fields readline sets itself while it reads a line (rltty.c,
# prepare_terminal_settings; measured on bash 5.3.9), with the value it sets: name ->
# (tcgetattr index, flag bit or None for a `cc` slot, `cc` index, value). FLUSHO is
# cleared by readline but Linux never keeps it set, so it only ever reads 0. Readline
# disables VDISCARD only when its key is bound to a readline function (`\C-o` is, by
# default), so the first prompt is not required to show it disabled.
_R_FIELDS = {
    "isig": (3, termios.ISIG, None, 1),
    "icanon": (3, termios.ICANON, None, 0),
    "echo": (3, termios.ECHO, None, 0),
    "flusho": (3, termios.FLUSHO, None, 0),
    "icrnl": (0, termios.ICRNL, None, 0),
    "inlcr": (0, termios.INLCR, None, 0),
    "istrip": (0, termios.ISTRIP, None, 0),
    "inpck": (0, termios.INPCK, None, 0),
    "min": (6, None, termios.VMIN, 1),
    "time": (6, None, termios.VTIME, 0),
    "lnext": (6, None, termios.VLNEXT, 0),  # 0: disabled (_POSIX_VDISABLE on Linux)
    "discard": (6, None, termios.VDISCARD, 0),
}
_R_FORCED = {name: field[3] for name, field in _R_FIELDS.items()}
_R_CONDITIONAL = frozenset({"discard"})


class SessionError(Exception):
    """The session cannot do what was asked."""


class SessionDegraded(SessionError):
    """The session is in degraded mode (§4.2); ``reason`` says why and ``code`` which check
    fired (§4.2). ``result`` is the command's result when its own snapshot degraded the
    session (``H`` changed)."""

    def __init__(
        self, reason: str, result: "CommandResult | None" = None, code: str = "other"
    ):
        super().__init__(f"session degraded: {reason}")
        self.reason = reason
        self.code = code
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
        tiocsti_path: str = TIOCSTI_PATH,
    ):
        """Prefer :meth:`start`. ``on_record`` sees every control record.

        ``proc_root`` is where bash's ``<pid>/syscall``, ``stat``, ``fd`` and ``fdinfo``
        are read (the prompt rule, the stopped check and the outside checks, §3-§4.2); if
        one cannot be read the session degrades with a reason naming the file.
        ``tiocsti_path`` is read once at start: unless it holds ``0`` the session starts
        degraded.
        """
        if max_output < 1:
            raise ValueError("max_output must be at least 1")
        self.max_output = max_output
        self.proc_root = proc_root
        self.tiocsti_path = tiocsti_path
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
        # The outside readings at start (spec §3, outside checks): the terminal settings
        # while readline holds the terminal at the first prompt, and bash's fds 0-2.
        self._prompt_termios: list | None = None
        self._fds: tuple | None = None

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
        self._prompt_termios = self._fds = None
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
                self._raise_if_degraded()
        self._remove_rcfile()  # bash has read it
        self._start_checks()
        # Fail closed at start if the prompt rule cannot be applied (§4.2 third trigger).
        if self._wait_for_prompt() and not self._check_stopped():
            self._prompt_baseline()
        self._check_tiocsti()

    def _start_checks(self) -> None:
        """Checks on the baseline that only make sense once: what ~/.bashrc left that the
        rcfile could not take back (spec §3)."""
        baseline = self._tracker.baseline
        assert baseline is not None
        lines = baseline.hooks.split("\n")
        unpinned = sorted(
            name[len(_UNPINNED) :] for name in baseline.functions if name.startswith(_UNPINNED)
        )
        ps_lines = [line for line in lines if re.match(r"declare -\S+ PS[12](=|$)", line)]
        if _FUNCTRACE_ON in lines:
            self._tracker.degrade(
                "functrace is on after ~/.bashrc (set -T, or extdebug): a DEBUG trap set"
                " later could hide itself from the snapshot and run inside nl-shell's hooks"
            )
        elif unpinned:
            self._tracker.degrade(
                f"{' and '.join(unpinned)} already readonly after ~/.bashrc, so nl-shell"
                " could not pin it (PS4 runs code when bash traces its hooks; with"
                " MAILCHECK set bash's mail check runs MAILPATH messages after the hooks)"
            )
        elif any(not re.match(r"declare -[-x] PS[12]=", line) for line in ps_lines):
            self._tracker.degrade(
                "PS1 or PS2 could not be reset after ~/.bashrc (readonly, or another"
                " attribute): bash expands it after the snapshot, before readline"
            )

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
    def degraded_code(self) -> str | None:
        """Which check degraded the session (§4.2), None while healthy."""
        return self._tracker.degraded_code

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
        # The outside checks while readline holds the terminal (spec §3, part 2), just
        # before the write. A change made after this reading is a known gap (#147).
        if not self._check_stopped():
            self._prompt_check()
        self._raise_if_degraded()
        self._tracker.command_sent()
        _write_all(self._proc.fd, PASTE_START + data + PASTE_END + b"\r")
        return self._wait_for_result(timeout)

    def _raise_if_degraded(self) -> None:
        if self._tracker.degraded:
            raise SessionDegraded(
                self._tracker.degraded_reason or "unknown",
                code=self._tracker.degraded_code or "other",
            )

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
                        # Before the output: with fd 1 redirected the end marker never
                        # reaches the terminal.
                        if not self._tracker.degraded:
                            self._check_fds_after_e()
                        if self._tracker.started and not self._tracker.degraded:
                            self._await_output_end(output)
                if finished is not None:
                    self._drain_control()  # a record after E degrades, checked below

            if self._tracker.degraded:
                result = None
                snapshot = self._tracker.snapshot
                if done is not None and snapshot is not None and snapshot.seq == done.seq:
                    result = self._result(done, snapshot, output)
                raise SessionDegraded(
                    self._tracker.degraded_reason or "unknown",
                    result,
                    code=self._tracker.degraded_code or "other",
                )
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
                        else "the snapshot did not finish after the shell returned to a prompt",
                        "missing-record",
                    )
                    continue
            else:
                waiting_since = None
            if self._check_stopped():
                continue
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
            if self._check_stopped():
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

    # ----- the outside checks (§3) and the stopped / TIOCSTI checks (§4.2) --

    def _prompt_baseline(self) -> None:
        """At the first prompt, readline holding the terminal: take the baseline of the
        terminal settings and of bash's fds 0-2. R must read as readline sets it, or the
        pre-paste reading could not tell readline's settings from the shell's."""
        settings = self._read_termios()
        fds = self._read_fds()
        if settings is None or fds is None:
            return
        self._prompt_termios, self._fds = settings, fds
        values = _r_values(settings)
        expected = {
            name: values[name] if name in _R_CONDITIONAL else value
            for name, value in _R_FORCED.items()
        }
        if values != expected:
            self._tracker.degrade(
                "the terminal settings at the first prompt do not have the values readline"
                " sets while it reads a line: " + _r_diff(expected, values),
                "outside-check",
            )

    def _prompt_check(self) -> None:
        """Just before a paste, while readline holds the terminal (part 2 of spec §3):
        compare every terminal setting with the first prompt's, taken in the same state, and
        bash's fds 0-2 with the baseline. The fields of R read as readline sets them here,
        never as the shell's own settings; a difference in them means something changed
        the terminal while readline held it. The shell's own values of R are not checked
        at all (#149): readline forces them while it reads a paste, so they cannot change
        its bytes."""
        settings = self._read_termios()
        if settings is None:
            return
        if self._prompt_termios is None:
            self._tracker.degrade(
                "no baseline of the terminal settings at the prompt", "outside-check"
            )
        elif settings != self._prompt_termios:
            self._tracker.degrade(
                "the terminal settings before the paste (readline holding the terminal)"
                " differ from the first prompt's: "
                + _termios_diff(self._prompt_termios, settings),
                "outside-check",
            )
        else:
            self._check_fds()

    def _check_fds(self) -> None:
        """bash's fds 0-2 must be what they were at the first prompt."""
        fds = self._read_fds()
        if fds is not None:
            self._fds_match(fds)

    def _check_fds_after_e(self) -> None:
        """At E: bash's fds 0-2 must be what they were at the first prompt. bash writes E
        with ``printf 'E\\n' >&3``, so for a moment after E arrives its fd 1 can still be
        the control pipe, until bash undoes that redirect. A reading that differs is
        therefore taken again once bash is back at its prompt (readline waiting for a
        line, so the redirect is undone), or after the prompt wait runs out."""
        fds = self._read_fds()
        if fds is None or fds == self._fds:
            return
        deadline = time.monotonic() + _SETTLE
        while True:
            try:
                if self._reading_terminal():
                    break
            except _ProcUnreadable as exc:
                self._tracker.degrade(str(exc))
                return
            if self._check_stopped():
                return
            if time.monotonic() > deadline:
                break  # e.g. fd 0 redirected: bash never waits on the terminal again
            time.sleep(0.002)
        self._check_fds()

    def _read_termios(self) -> list | None:
        """The PTY's terminal settings, read through the master (Linux returns the
        slave's); None (degraded) if they cannot be read."""
        assert self._proc is not None
        try:
            return termios.tcgetattr(self._proc.fd)
        except (termios.error, OSError) as exc:
            self._tracker.degrade(
                f"cannot read the terminal settings of the session's PTY ({exc})",
                "outside-check",
            )
            return None

    def _read_fds(self) -> tuple | None:
        """What bash's fds 0, 1 and 2 are (file identity and access mode), from
        ``/proc``; None (degraded) if that cannot be read."""
        assert self._proc is not None
        base = os.path.join(self.proc_root, str(self._proc.pid))
        found = []
        for fd in (0, 1, 2):
            path = os.path.join(base, "fd", str(fd))
            try:
                info = os.stat(path)
                info_path = os.path.join(base, "fdinfo", str(fd))
                with open(info_path, encoding="ascii") as fh:
                    flags = next(
                        line.split()[1] for line in fh if line.startswith("flags:")
                    )
                mode = int(flags, 8) & os.O_ACCMODE
            except (OSError, UnicodeDecodeError, StopIteration, IndexError, ValueError) as exc:
                self._tracker.degrade(
                    f"cannot read {path} or its fdinfo ({exc.__class__.__name__}), so the"
                    " outside check cannot be applied",
                    "outside-check",
                )
                return None
            found.append(
                (info.st_dev, info.st_ino, info.st_rdev, stat.S_IFMT(info.st_mode), mode)
            )
        return tuple(found)

    def _fds_match(self, fds: tuple) -> bool:
        """``fds`` equal the baseline's; else degrade, naming the first fd that differs."""
        assert self._fds is not None and self._proc is not None
        for fd, (now, then) in enumerate(zip(fds, self._fds)):
            if now != then:
                path = os.path.join(self.proc_root, str(self._proc.pid), "fd", str(fd))
                try:
                    target = os.readlink(path)
                except OSError:
                    target = "unknown"
                self._tracker.degrade(
                    f"the shell's fd {fd} is not what it was at the first prompt"
                    f" (now {target})",
                    "outside-check",
                )
                return False
        return True

    def _check_stopped(self) -> bool:
        """Degrade (and return True) if bash is stopped (state ``T`` or ``t`` in
        ``/proc/<pid>/stat``) or that cannot be read."""
        assert self._proc is not None
        path = os.path.join(self.proc_root, str(self._proc.pid), "stat")
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            self._tracker.degrade(
                f"cannot read {path} ({exc.__class__.__name__}), so whether the shell is"
                " stopped cannot be checked",
                "stopped",
            )
            return True
        end = text.rfind(")")
        state = text[end + 2 : end + 3] if end >= 0 else ""
        if state in ("T", "t"):
            self._tracker.degrade(f"the shell is stopped (state {state} in {path})", "stopped")
            return True
        return False

    def _check_tiocsti(self) -> None:
        """Start degraded unless ``tiocsti_path`` holds ``0``: otherwise another process
        could push bytes into the session's terminal input (TIOCSTI)."""
        if self._tracker.degraded:
            return
        path = self.tiocsti_path
        try:
            with open(path, encoding="ascii") as fh:
                value = fh.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            self._tracker.degrade(
                f"cannot read {path} ({exc.__class__.__name__}), so whether another process"
                " may push input into the session's terminal (TIOCSTI) is unknown",
                "tiocsti",
            )
            return
        if value != "0":
            self._tracker.degrade(
                f"{path} is {value!r}, not '0': TIOCSTI is allowed, so another process"
                " could push input into the session's terminal",
                "tiocsti",
            )

    # ----- helpers --

    def _read_pty(self) -> bytes:
        assert self._proc is not None
        try:
            chunk = os.read(self._proc.fd, 65536)
        except OSError:
            chunk = b""  # EIO: bash has exited and the slave side is gone
        if not chunk:
            self._tracker.degrade("the shell exited", "exited")
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


def _cc_value(char: bytes | int) -> int:
    return char[0] if isinstance(char, bytes) else char


def _r_values(settings: list) -> dict[str, int]:
    """The fields of R (``_R_FIELDS``) in a ``tcgetattr`` result."""
    values = {}
    for name, (index, bit, cc, _) in _R_FIELDS.items():
        if bit is not None:
            values[name] = int(bool(settings[index] & bit))
        else:
            values[name] = _cc_value(settings[index][cc])
    return values


def _r_diff(before: dict[str, int], after: dict[str, int]) -> str:
    """Which fields of R differ, e.g. ``isig 1 -> 0``."""
    return ", ".join(
        f"{name} {before[name]} -> {after[name]}" for name in before if before[name] != after[name]
    )


def _termios_diff(before: list | None, after: list) -> str:
    """Which ``tcgetattr`` fields differ, e.g. ``lflag 0x8a3b -> 0x8a3a``."""
    if before is None:
        return "no baseline"
    parts = []
    for name, old, new in zip(_TERMIOS_FIELDS, before, after):
        if old == new:
            continue
        if name == "cc":
            changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
            parts.append(f"cc at {', '.join(map(str, changed))}")
        else:
            parts.append(f"{name} {old:#x} -> {new:#x}")
    return ", ".join(parts)


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
