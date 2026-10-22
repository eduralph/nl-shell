"""The character-safety policy for proposals and printed text (#144).

Every vector is fed as a stub response (or typed as a request) through the real ``Repl``
with the recording stub backend, so the tests drive the production parser and escape
path. ``nl_shell`` modules other than ``nl_shell.repl`` are imported inside the tests, so
the file still loads (and fails) on a tree without this issue's change. Every non-ASCII
character in this file is written as a Python escape, never pasted, so the source shows
exactly what it tests. Expected escapes and reply texts are pinned as literals.
"""

import importlib.util
import io
import json
import unicodedata
from pathlib import Path

import pytest

from nl_shell.repl import Repl

REPO = Path(__file__).resolve().parents[1]
UCD = Path("/usr/share/unicode")

NO_USABLE = "No usable proposal came back from the model."
NOTHING_RUNS_YET = (
    "Nothing runs yet: running a proposal arrives with the policy gate (#12, #78)."
)
DF = '{"kind": "command", "command": "df -h", "risk": "read-only"}'
DF_SHOWN = (
    "Proposed command (risk: read-only, 1 line):\n"
    "  | df -h\n"
    f"{NOTHING_RUNS_YET}\n"
)
ACUTE = "\u0301"  # COMBINING ACUTE ACCENT (Mn)


def test_this_file_is_ascii():
    # The module docstring promises every non-ASCII character is a Python escape, so an
    # editor that turns NBSP into a space or drops a variation selector cannot silently
    # change what a row tests.
    Path(__file__).read_bytes().decode("ascii")


def scripted(*lines):
    it = iter(lines)

    def read(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


@pytest.fixture(autouse=True)
def no_commands_on_path(tmp_path, monkeypatch):
    """An empty ``$PATH``, so `show ...` routes as natural language on any host."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def session(*lines, responses=()):
    """Run a Repl with the recording stub; returns (stub, printed text)."""
    from nl_shell import prompts, targets
    from nl_shell.backend import RecordingStub

    target = targets.load("ubuntu-26.04")
    loaded = prompts.load(target.folder, prompts.user_path())
    stub = RecordingStub(responses)
    out = io.StringIO()
    repl = Repl(scripted(*lines), out, backend=stub, prompts=loaded)
    assert repl.run() == 0
    return stub, out.getvalue()


def command_response(command, *, ensure_ascii=True):
    return json.dumps(
        {"kind": "command", "command": command, "risk": "read-only"},
        ensure_ascii=ensure_ascii,
    )


def shown(command):
    lines = command.split("\n")
    count = f"{len(lines)} line" + ("s" if len(lines) > 1 else "")
    return (
        f"Proposed command (risk: read-only, {count}):\n"
        + "".join(f"  | {line}\n" for line in lines)
        + f"{NOTHING_RUNS_YET}\n"
    )


def assert_rejected(command):
    """The proposal is refused, and the next request still gets a proposal."""
    for ensure_ascii in (True, False):
        stub, out = session(
            "show disk usage",
            "@list files",
            responses=[command_response(command, ensure_ascii=ensure_ascii), DF],
        )
        assert out == NO_USABLE + "\n" + DF_SHOWN, (command, ensure_ascii)
        assert len(stub.inputs) == 2


def assert_accepted(command):
    """The proposal is shown exactly as the model sent it."""
    for ensure_ascii in (True, False):
        _, out = session(
            "show disk usage",
            responses=[command_response(command, ensure_ascii=ensure_ascii)],
        )
        assert out == shown(command), (command, ensure_ascii)


def context_line(typed):
    """The last line ``:context`` prints after the request ``typed``."""
    stub, out = session(typed, ":context", responses=[DF])
    assert stub.inputs[0][-1][1].endswith(typed)  # the model is sent the raw text
    return out, out.splitlines()[-1]


# (1) characters that render as nothing or as blank ------------------------------------

HIDDEN = {
    "hangul-filler-u3164": ("\u3164", "\\u3164"),
    "hangul-choseong-filler-u115f": ("\u115f", "\\u115f"),
    "hangul-jungseong-filler-u1160": ("\u1160", "\\u1160"),
    "halfwidth-hangul-filler-uffa0": ("\uffa0", "\\uffa0"),
    "combining-grapheme-joiner-u034f": ("\u034f", "\\u034f"),
    "variation-selector-1-ufe00": ("\ufe00", "\\ufe00"),
    "variation-selector-16-ufe0f": ("\ufe0f", "\\ufe0f"),
    "variation-selector-17-ue0100": ("\U000e0100", "\\U000e0100"),
    "khmer-inherent-aq-u17b4": ("\u17b4", "\\u17b4"),
    "mongolian-fvs1-u180b": ("\u180b", "\\u180b"),
    "braille-blank-u2800": ("\u2800", "\\u2800"),
    "musical-null-notehead-u1d159": ("\U0001d159", "\\U0001d159"),
    "khitan-filler-u16fe4": ("\U00016fe4", "\\U00016fe4"),
    "egyptian-full-blank-u13441": ("\U00013441", "\\U00013441"),
    "egyptian-half-blank-u13442": ("\U00013442", "\\U00013442"),
    "no-break-space-u00a0": ("\xa0", "\\xa0"),
    "ideographic-space-u3000": ("\u3000", "\\u3000"),
    "private-use-ue000": ("\ue000", "\\ue000"),
    "unassigned-u0378": ("\u0378", "\\u0378"),
}


@pytest.mark.parametrize(("char", "escaped"), HIDDEN.values(), ids=HIDDEN.keys())
def test_command_with_a_hidden_character_is_rejected(char, escaped):
    assert_rejected(f"ls{char} -la")


@pytest.mark.parametrize(("char", "escaped"), HIDDEN.values(), ids=HIDDEN.keys())
def test_hidden_character_inside_quotes_is_rejected(char, escaped):
    assert_rejected(f"echo '{char}'")
    assert_rejected(f'echo "a{char}b"')


@pytest.mark.parametrize(("char", "escaped"), HIDDEN.values(), ids=HIDDEN.keys())
def test_hidden_character_in_a_request_is_escaped_in_context(char, escaped):
    out, last = context_line(f"show{char}disk usage")
    assert last.endswith(f"show{escaped}disk usage")
    assert char not in out


# (2) combining marks: at most two in a row ---------------------------------------------


def test_three_combining_marks_after_a_letter_are_rejected():
    assert_rejected("echo e" + ACUTE * 3)


def test_two_combining_marks_after_a_letter_are_accepted():
    assert_accepted("echo e" + ACUTE * 2)


def test_three_combining_marks_at_the_start_are_rejected():
    assert_rejected(ACUTE * 3 + "echo hi")


def test_two_combining_marks_at_the_start_are_accepted():
    assert_accepted(ACUTE * 2 + "echo hi")


def test_three_combining_marks_after_a_line_feed_are_rejected():
    assert_rejected("echo hi\n" + ACUTE * 3)


def test_third_combining_mark_is_escaped_in_context():
    out, last = context_line("show e" + ACUTE * 3 + " disk")
    assert last.endswith("show e" + ACUTE + ACUTE + "\\u0301" + " disk")
    assert ACUTE * 3 not in out


# (3) letters of other scripts and lightly combined letters are shown as they are ------

ACCEPTED = {
    # "Dokumenty" in Cyrillic; "archeio" in Greek; "nihongo fairu" in Han + Katakana.
    "cyrillic-word":"ls \u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b",
    "greek-word": "cat \u03b1\u03c1\u03c7\u03b5\u03af\u03bf.txt",
    "han-and-katakana": "cat \u65e5\u672c\u8a9e\u30d5\u30a1\u30a4\u30eb.txt",
    "precomposed-e-acute": "echo caf\xe9",
    "e-and-one-combining-acute": "echo cafe" + ACUTE,
}


@pytest.mark.parametrize("command", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_ordinary_command_is_accepted_and_shown_unchanged(command):
    assert_accepted(command)


# (4) the vendored tables, not the interpreter's unicodedata ---------------------------


def test_tables_record_unicode_16():
    from nl_shell import _unicode_tables

    assert _unicode_tables.UNICODE_VERSION == "16.0.0"


def test_policy_modules_never_use_unicodedata():
    # The monkeypatch below cannot see `from unicodedata import category`; this can.
    for name in ("nl_shell.render", "nl_shell.proposal", "nl_shell._unicode_tables"):
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, name
        assert "unicodedata" not in Path(spec.origin).read_text(encoding="utf-8"), name


def test_policy_reads_the_vendored_tables_not_unicodedata(monkeypatch):
    # Make the interpreter's database disagree with Unicode 16.0 on every property the
    # policy uses: U+0378 assigned (not Cn), U+1C89 CYRILLIC CAPITAL LETTER TJE (new in
    # 16.0) unassigned, U+0301 COMBINING ACUTE ACCENT a letter (not Mn), U+3164 HANGUL
    # FILLER (Default_Ignorable) an ordinary letter. The policy must not notice.
    real_category = unicodedata.category
    faked = {"\u0378": "Lu", "\u1c89": "Cn", "\u0301": "Lo", "\u3164": "Lo"}

    def category(char):
        return faked.get(char, real_category(char))

    monkeypatch.setattr(unicodedata, "category", category)
    monkeypatch.setattr(unicodedata, "combining", lambda char: 0)
    monkeypatch.setattr(unicodedata, "unidata_version", "15.0.0")
    assert_rejected("ls \u0378")
    assert_accepted("ls \u1c89")
    assert_rejected("echo e" + ACUTE * 3)
    assert_rejected("ls\u3164 -la")


# (5) the committed tables are what the generator makes from the UCD -------------------


def ucd_skip_reason():
    derived = UCD / "DerivedCoreProperties.txt"
    if not derived.is_file():
        return f"{derived} is absent (Ubuntu package unicode-data)"
    with derived.open(encoding="utf-8") as handle:
        first = handle.readline().rstrip("\n")
    if first != "# DerivedCoreProperties-16.0.0.txt":
        return f"{derived} is not Unicode 16.0.0 (first line {first!r})"
    for name in ("PropList.txt", "UnicodeData.txt"):
        if not (UCD / name).is_file():
            return f"{UCD / name} is absent"
    return None


def test_committed_tables_match_the_generator():
    reason = ucd_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    script = REPO / "scripts" / "gen_unicode_tables.py"
    spec = importlib.util.spec_from_file_location("gen_unicode_tables", script)
    assert spec is not None and spec.loader is not None, script
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    committed = REPO / "src" / "nl_shell" / "_unicode_tables.py"
    assert gen.generate(UCD, "16.0.0") == committed.read_text(encoding="utf-8")
