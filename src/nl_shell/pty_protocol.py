"""pty-protocol v1 (``docs/design/specs/pty-protocol/v2.md``): records (§2), the snapshot
(§3), sequencing and degraded mode (§4) and which commands may be sent (§5).

Pure logic with no I/O: ``session.py`` feeds it the bytes read from the control channel
(fd 3 in bash). Exit codes and completion come only from here, never from PTY text.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from typing import Callable

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"

# A record longer than this is malformed rather than buffered forever.
MAX_RECORD = 1 << 20

# Record types every snapshot carries (§3), in no required order.
SNAPSHOT_TYPES = ("F", "L", "T", "B", "X", "P", "J", "H", "V")
KNOWN_TYPES = frozenset(("S", "D", "E") + SNAPSHOT_TYPES)

# At most 18 digits: int() raises past 4300, and a malformed record must degrade (§4.2).
_NUM = r"\d{1,18}"
_NUMERIC = {
    "S": re.compile(_NUM),
    "D": re.compile(rf"{_NUM} {_NUM}(?: {_NUM})*"),
    "J": re.compile(_NUM),
}
# The one reversible free-text encoding (§2).
_ESCAPES = {"\\": "\\", "n": "\n", "r": "\r"}


class ProtocolError(Exception):
    """A control-channel record that does not follow pty-protocol v1 §2."""


class CommandRefused(ValueError):
    """A command that pty-protocol v1 §5.2 forbids sending; nothing was written to the PTY."""


@dataclasses.dataclass(frozen=True)
class Record:
    type: str
    fields: str  # everything after "<type> ", "" when there is nothing


@dataclasses.dataclass(frozen=True)
class Done:
    """A ``D`` record: the command finished, or nothing ran (§2, §4.1)."""

    seq: int
    status: int
    pipestatus: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """The session state reported before a prompt (§3). Text fields are decoded."""

    seq: int
    functions: tuple[str, ...]  # F: `compgen -A function`, one name per line
    aliases: str  # L: `alias -p`
    traps: str  # T: `trap -p`
    disabled_builtins: str  # B: `enable -n`
    extdebug: str  # X: `shopt -p extdebug`
    path: str  # P: `$PATH`
    stopped_jobs: int  # J: number of stopped jobs
    hooks: str  # H: `declare -p` / `declare -f` text of nl-shell's hooks and their state
    variables: str  # V: `declare -p` text of the pager and loader variables


class Phase(enum.Enum):
    BASELINE = "waiting for the first snapshot"
    IDLE = "idle"
    SENT = "waiting for the start record"
    RUNNING = "waiting for the done record"
    SNAPSHOT = "reading the snapshot"
    DEGRADED = "degraded"


def decode_field(text: str) -> str:
    """Undo the §2 free-text encoding. Raises :class:`ProtocolError` on a bad escape."""
    if "\\" not in text:
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt not in _ESCAPES:
            raise ProtocolError(f"bad escape {text[i:i + 2]!r} in a free-text field")
        out.append(_ESCAPES[nxt])
        i += 2
    return "".join(out)


def parse_record(line: bytes) -> Record:
    """Parse one record, without its trailing ``\\n``. Raises :class:`ProtocolError`."""
    if b"\r" in line:
        raise ProtocolError("record contains a carriage return")
    if len(line) > MAX_RECORD:
        raise ProtocolError("record is too long")
    text = line.decode("utf-8", "surrogateescape")
    if not text or not ("A" <= text[0] <= "Z"):
        raise ProtocolError(f"record does not start with a one-letter type: {text[:40]!r}")
    if len(text) > 1 and text[1] != " ":
        raise ProtocolError(f"record type is longer than one letter: {text[:40]!r}")
    rtype, fields = text[0], text[2:]
    if rtype == "E" and len(text) != 1:
        raise ProtocolError("E record has fields")
    pattern = _NUMERIC.get(rtype)
    if pattern is not None and not pattern.fullmatch(fields):
        raise ProtocolError(f"malformed {rtype} record: {text[:80]!r}")
    if rtype in SNAPSHOT_TYPES and rtype != "J":
        decode_field(fields)  # validates the escapes
    return Record(rtype, fields)


def _seq_line(seq: int) -> str:
    """The end of the ``declare -p __nls_seq`` line in ``H`` when the counter is ``seq``."""
    return f' __nls_seq="{seq}"\n'


def expected_hooks(baseline: Snapshot, seq: int) -> str:
    """The ``H`` a healthy session reports at ``seq``: the baseline's, byte for byte, with
    the sequence counter's value set to ``seq`` (§3, §4.2)."""
    return baseline.hooks.replace(
        _seq_line(baseline.seq), _seq_line(seq), 1
    )


def check_command(command: str | bytes) -> bytes:
    """The bytes to paste for ``command``, or :class:`CommandRefused` (§5.2): no paste
    end, ESC or other C0 control except TAB and LF. An empty command is allowed (§4.1)."""
    data = command.encode("utf-8") if isinstance(command, str) else bytes(command)
    if PASTE_END in data:
        raise CommandRefused("command contains the paste-end sequence ESC [201~")
    for byte in data:
        if byte < 0x20 and byte not in (0x09, 0x0A):
            raise CommandRefused(f"command contains the control character 0x{byte:02x}")
    return data


class ProtocolTracker:
    """The §4 state machine. ``feed`` takes raw pipe bytes and returns the events they
    completed: ``("S", n)``, ``("D", Done)``, ``("E", Snapshot)``. Degraded mode is sticky
    and keeps the first reason and its code (``degraded_code``, spec §4.2: which check
    fired). Unknown record types are ignored (§Versioning)."""

    def __init__(self, on_record: Callable[[bytes], None] | None = None):
        self.phase = Phase.BASELINE
        self.seq = 0  # sequence number of the last finished command
        self.baseline: Snapshot | None = None
        self.snapshot: Snapshot | None = None
        self.done: Done | None = None
        self.started = False  # an `S` came for the command in flight
        self.degraded_reason: str | None = None
        self.degraded_code: str | None = None
        self._on_record = on_record
        self._buf = bytearray()
        self._fields: dict[str, str] = {}

    @property
    def degraded(self) -> bool:
        return self.phase is Phase.DEGRADED

    def degrade(self, reason: str, code: str = "other") -> None:
        if self.phase is not Phase.DEGRADED:
            self.phase = Phase.DEGRADED
            self.degraded_reason = reason
            self.degraded_code = code

    def command_sent(self) -> None:
        if self.phase is not Phase.IDLE:
            raise RuntimeError(f"cannot send a command while {self.phase.value}")
        self.done = None
        self.started = False
        self.phase = Phase.SENT

    def eof(self) -> None:
        self.degrade("the control channel closed")

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        if self.degraded:
            return events
        self._buf += data
        while not self.degraded:
            end = self._buf.find(b"\n")
            if end < 0:
                if len(self._buf) > MAX_RECORD:
                    self.degrade("malformed control record: record is too long")
                break
            line = bytes(self._buf[: end + 1])
            del self._buf[: end + 1]
            if self._on_record is not None:
                self._on_record(line)
            event = self._handle(line[:-1])
            if event is not None:
                events.append(event)
        return events

    def _handle(self, line: bytes) -> tuple[str, object] | None:
        try:
            return self._sequence(parse_record(line))
        except (ProtocolError, ValueError) as exc:
            self.degrade(f"malformed control record: {exc}")
            return None

    def _sequence(self, record: Record) -> tuple[str, object] | None:
        rtype = record.type
        if rtype not in KNOWN_TYPES:
            return None
        phase = self.phase
        nxt = self.seq + 1
        if phase is Phase.BASELINE:
            if rtype == "D" and self._done(record).seq == 0:
                self.phase = Phase.SNAPSHOT
                return None
            return self._out_of_order(rtype, "before the first snapshot")
        if phase is Phase.IDLE:
            return self._out_of_order(rtype, "while no command was running")
        if phase is Phase.SENT:
            if rtype == "S" and int(record.fields) == nxt:
                self.started = True
                self.phase = Phase.RUNNING
                return ("S", nxt)
            if rtype == "D":
                # §4.1: `D n+1` without `S n+1`: nothing ran.
                return self._finish(record, nxt)
            return self._out_of_order(rtype, f"while waiting for 'S {nxt}'")
        if phase is Phase.RUNNING:
            if rtype == "D":
                return self._finish(record, nxt)
            if rtype == "S" and int(record.fields) == nxt:
                return None  # the next line of the same multi-line command
            return self._out_of_order(rtype, f"while waiting for 'D {nxt}'")
        # Phase.SNAPSHOT
        if rtype in SNAPSHOT_TYPES:
            if rtype in self._fields:
                return self._out_of_order(rtype, "twice in one snapshot")
            self._fields[rtype] = record.fields
            return None
        if rtype == "E":
            return self._end_snapshot()
        return self._out_of_order(rtype, "inside a snapshot")

    def _finish(self, record: Record, nxt: int) -> tuple[str, object] | None:
        done = self._done(record)
        if done.seq != nxt:
            return self._out_of_order("D", f"with sequence number {done.seq}, expected {nxt}")
        self.done = done
        self.phase = Phase.SNAPSHOT
        return ("D", done)

    def _out_of_order(self, rtype: str, where: str) -> None:
        self.degrade(f"control record '{rtype}' arrived out of order ({where})")
        return None

    @staticmethod
    def _done(record: Record) -> Done:
        numbers = [int(n) for n in record.fields.split(" ")]
        return Done(numbers[0], numbers[1], tuple(numbers[2:]))

    def _end_snapshot(self) -> tuple[str, object] | None:
        fields, self._fields = self._fields, {}
        missing = [t for t in SNAPSHOT_TYPES if t not in fields]
        if missing:
            self.degrade(f"snapshot is missing record(s) {', '.join(missing)}", "missing-record")
            return None
        text = {t: decode_field(v) for t, v in fields.items() if t != "J"}
        snapshot = Snapshot(
            seq=self.done.seq if self.done is not None else 0,
            functions=tuple(name for name in text["F"].split("\n") if name),
            aliases=text["L"],
            traps=text["T"],
            disabled_builtins=text["B"],
            extdebug=text["X"],
            path=text["P"],
            stopped_jobs=int(fields["J"]),
            hooks=text["H"],
            variables=text["V"],
        )
        self.snapshot = snapshot
        if self.baseline is None:
            if snapshot.hooks.count(_seq_line(snapshot.seq)) != 1:
                self.degrade("the baseline's H does not report the sequence counter")
                return None
            self.baseline = snapshot
        elif snapshot.hooks != expected_hooks(self.baseline, snapshot.seq):
            self.degrade(
                "nl-shell's hooks changed (PROMPT_COMMAND, PS0, PS1/PS2, TERM, promptvars,"
                " the hook functions, their state variables or the saved user hook):"
                " the snapshot's H differs from the baseline",
                "h-changed",
            )
            return None
        self.seq = snapshot.seq
        self.phase = Phase.IDLE
        return ("E", snapshot)
