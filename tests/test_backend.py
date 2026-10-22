"""Prompts YAML, `:context`, the strict proposal/v1 parser and the recording stub (#143).

Natural-language input and `@request` are driven through the real ``Repl`` with the
recording stub backend; start-up is driven through the real ``cli.main``. The modules this
issue adds are imported inside the tests, never at module level, so the file still loads
(and fails) on a tree without them. Reply texts are pinned as literals.

The `:context` text form is rebuilt here, independently of the module's serializer, from
the brief's Design: each message is ``"--- " + role + " ---\\n" + escape(content)``, joined
with LF, no trailing LF; ``escape`` writes hidden characters as ``\\xHH`` / ``\\uHHHH`` /
``\\UHHHHHHHH`` (lowercase) and a backslash as ``\\\\``.
"""

import io
import json
import os
import unicodedata
from pathlib import Path

import pytest

from nl_shell.repl import Repl

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS = REPO / "targets" / "ubuntu-26.04" / "prompts" / "prompts.yaml"
UBUNTU_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="26.04"\n'

NO_BACKEND = (
    "No model backend is configured, so nl-shell can't answer this yet: "
    "the model client arrives with #77."
)
NO_USABLE = "No usable proposal came back from the model."
NO_MODEL_INPUT_YET = (
    "No model input yet: :context shows the model input of the last request."
)
NOTHING_RUNS_YET = (
    "Nothing runs yet: running a proposal arrives with the policy gate (#12, #78)."
)
CLARIFY = (
    "The model needs more from you before it can propose a command: "
    "some information is missing."
)
REFUSE = "The model will not propose a command for this: this is not a shell task."

DF = '{"kind": "command", "command": "df -h", "risk": "read-only"}'
DF_SHOWN = (
    "Proposed command (risk: read-only, 1 line):\n"
    "  | df -h\n"
    f"{NOTHING_RUNS_YET}"
)


# The test's own oracle for the `:context` text form (see the module docstring).
BIDI = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def oracle_hidden(char):
    code = ord(char)
    if char in "\n\t":
        return False
    return (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or code in BIDI
        or code in (0x2028, 0x2029)
        or 0xE0000 <= code <= 0xE007F
        or 0xD800 <= code <= 0xDFFF
        or unicodedata.category(char) == "Cf"
    )


def oracle_escape(text):
    out = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif not oracle_hidden(char):
            out.append(char)
        elif code <= 0xFF:
            out.append("\\x%02x" % code)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    return "".join(out)


def expected_context(recorded):
    return "\n".join(
        "--- " + role + " ---\n" + oracle_escape(content) for role, content in recorded
    )


def scripted(*lines):
    """A read function that returns ``lines`` in order, then signals end of input."""
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


def user_prompts_file():
    """The user's prompts.yaml under the suite's temporary XDG_CONFIG_HOME (conftest)."""
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "nl-shell" / "prompts.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def loaded_prompts():
    """The real target's prompts, found through the target loader, plus any user file."""
    from nl_shell import prompts, targets

    target = targets.load("ubuntu-26.04")
    return prompts.load(target.folder, prompts.user_path())


def session(*lines, responses=(), out=None):
    """Run a Repl with the recording stub; returns (stub, printed text)."""
    from nl_shell.backend import RecordingStub

    stub = RecordingStub(responses)
    out = io.StringIO() if out is None else out
    repl = Repl(scripted(*lines), out, backend=stub, prompts=loaded_prompts())
    assert repl.run() == 0
    return stub, out.getvalue()


def file_examples():
    """The default file's few-shot examples, read by the test itself."""
    import yaml

    return yaml.safe_load(DEFAULT_PROMPTS.read_text(encoding="utf-8"))


# (a) the model input and `:context` -----------------------------------------------------


@pytest.mark.parametrize(
    ("line", "typed"),
    [("show disk usage", "show disk usage"), ("@show disk usage", "show disk usage")],
    ids=["natural-language", "at-request"],
)
def test_context_prints_exactly_the_recorded_model_input(line, typed):
    stub, out = session(line, ":context", responses=[DF])
    assert len(stub.inputs) == 1
    recorded = stub.inputs[0]
    doc = file_examples()
    # Stable part first: the system prompt, then each example in file order.
    assert recorded[0] == ("system", doc["system"])
    examples = doc["examples"]
    assert examples, "the default prompts need few-shot examples for this test"
    pairs = recorded[1:-1]
    assert len(pairs) == 2 * len(examples)
    for index, example in enumerate(examples):
        user, assistant = pairs[2 * index], pairs[2 * index + 1]
        assert user[0] == "user" and user[1].endswith(example["request"])
        assert assistant == ("assistant", example["response"])
    # Volatile part last: the user message, ending with the request.
    assert recorded[-1][0] == "user"
    assert recorded[-1][1].endswith(typed)
    assert out == DF_SHOWN + "\n" + expected_context(recorded) + "\n"


def test_context_before_any_request_has_a_fixed_reply():
    stub, out = session(":context")
    assert stub.inputs == []
    assert out == NO_MODEL_INPUT_YET + "\n"


# (b) the stub records every input; ESC and a typed backslash in `:context` --------------


def test_stub_records_one_input_per_request_in_order():
    stub, _ = session("show disk usage", "@list my files", responses=[DF, DF])
    assert len(stub.inputs) == 2
    assert stub.inputs[0][-1][1].endswith("show disk usage")
    assert stub.inputs[1][-1][1].endswith("list my files")


def test_real_esc_and_typed_backslash_text_print_differently():
    esc = "show \x1b[2J disk"
    typed = "show \\x1b disk"  # five characters: backslash, x, 1, b
    stub, out = session(esc, ":context", typed, ":context", responses=[DF, DF])
    first, second = stub.inputs
    assert first[-1][1].endswith(esc)  # the backend got the raw ESC
    assert second[-1][1].endswith(typed)
    assert out == (
        DF_SHOWN + "\n" + expected_context(first) + "\n"
        + DF_SHOWN + "\n" + expected_context(second) + "\n"
    )
    assert "\x1b" not in out
    lines = out.splitlines()
    # The real ESC prints as the four characters \x1b; the typed text as \\x1b.
    assert any(line.endswith("show \\x1b[2J disk") for line in lines)
    assert any(line.endswith("show \\\\x1b disk") for line in lines)
    assert not any(line.endswith("show \\x1b disk") for line in lines)


# (c) the user's prompts.yaml, and malformed files at start-up ---------------------------


class Started:
    def __init__(self):
        self.repl = False
        self.reads = 0


def start(monkeypatch, capsys, *lines, responses=(), targets_root=None, release=None):
    """Run the real ``cli.main`` with a matching os-release; the REPL it builds gets the
    recording stub and reads ``lines``. Returns (exit code, stdout of the REPL, stderr,
    Started, stub)."""
    from nl_shell import cli
    from nl_shell.backend import RecordingStub

    stub = RecordingStub(responses)
    out = io.StringIO()
    started = Started()

    def read(prompt):
        started.reads += 1
        return scripted_lines(prompt)

    scripted_lines = scripted(*lines)

    def interactive(cls, status_lines=(), *, backend=None, prompts=None):
        started.repl = True
        return Repl(read, out, status_lines, backend=stub, prompts=prompts)

    monkeypatch.setattr(cli.Repl, "interactive", classmethod(interactive))
    kwargs = {"os_release_paths": [release]}
    if targets_root is not None:
        kwargs["targets_root"] = targets_root
    code = cli.main([], **kwargs)
    err = capsys.readouterr().err
    return code, out.getvalue(), err, started, stub


@pytest.fixture
def ubuntu_release(tmp_path):
    path = tmp_path / "os-release"
    path.write_text(UBUNTU_RELEASE, encoding="utf-8")
    return path


def test_user_prompts_file_overrides_the_system_prompt(
    monkeypatch, capsys, ubuntu_release
):
    user_prompts_file().write_text(
        "system: |-\n  The user's own system prompt.\n", encoding="utf-8"
    )
    code, out, err, started, stub = start(
        monkeypatch, capsys, "show disk usage", ":context",
        responses=[DF], release=ubuntu_release,
    )
    assert (code, err) == (0, "")
    recorded = stub.inputs[0]
    assert recorded[0] == ("system", "The user's own system prompt.")
    # The keys the user did not set come from the default.
    assert recorded[-1][1].endswith("show disk usage")
    assert len(recorded) == 2 + 2 * len(file_examples()["examples"])
    assert out == DF_SHOWN + "\n" + expected_context(recorded) + "\n"
    assert "--- system ---\nThe user's own system prompt.\n" in out


MALFORMED = {
    "invalid-yaml": "system: [unclosed\n",
    "not-a-mapping": "- system\n- examples\n",
    "wrong-type": "system: 5\n",
    "unknown-key": "colour: blue\n",
    "sequence-key": "? [system, examples]\n: x\n",
    # Each few-shot `response` must itself be a valid proposal/v1 answer.
    "invalid-example-response": (
        "examples:\n  - request: show disk usage\n    response: 'df -h'\n"
    ),
    # The request goes last (ADR-0018 §4): {state} after {request} is refused.
    "state-after-request": "request_template: '{request}{state}'\n",
    "template-not-ending-in-request": "request_template: '{state}{request} please'\n",
    # The safe loader raises ValueError (int() over the digit limit) and RecursionError
    # (deep nesting), not YAMLError; both must still refuse start-up, naming the file.
    "huge-integer": "system: " + "9" * 5000 + "\n",
    "deep-nesting": "system: " + "[" * 5000 + "]" * 5000 + "\n",
    "bad-timestamp": "system: 2024-13-45\n",
}


@pytest.mark.parametrize("text", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_user_prompts_refuse_start_up(monkeypatch, capsys, ubuntu_release, text):
    path = user_prompts_file()
    path.write_text(text, encoding="utf-8")
    code, out, err, started, stub = start(
        monkeypatch, capsys, "show disk usage", release=ubuntu_release
    )
    assert code != 0
    assert not started.repl and started.reads == 0
    assert str(path) in err
    assert "Traceback" not in err


def test_python_tag_in_user_prompts_is_not_run(
    monkeypatch, capsys, ubuntu_release, tmp_path
):
    pwned = tmp_path / "pwned"
    path = user_prompts_file()
    tag = f'!!python/object/apply:os.system ["touch {pwned}"]\n'
    path.write_text(tag, encoding="utf-8")
    code, out, err, started, stub = start(
        monkeypatch, capsys, "show disk usage", release=ubuntu_release
    )
    assert code != 0
    assert not started.repl and started.reads == 0
    assert str(path) in err
    assert not pwned.exists()


def test_bidi_control_in_a_broken_prompts_line_is_escaped(
    monkeypatch, capsys, ubuntu_release
):
    path = user_prompts_file()
    path.write_text("system: [broken ‮ here\n", encoding="utf-8")
    code, out, err, started, stub = start(monkeypatch, capsys, release=ubuntu_release)
    assert code != 0
    assert not started.repl
    assert str(path) in err
    assert "broken \\u202e here" in err  # the broken line is quoted, escaped
    assert "‮" not in err


@pytest.mark.parametrize(
    "shell_line",
    ['shell = "‮bash"', "shell = ‮bash"],
    ids=["bad-value", "bad-toml-syntax"],
)
def test_bidi_control_in_a_broken_target_toml_is_escaped(
    monkeypatch, capsys, tmp_path, shell_line
):
    # U+202E sits in the broken `shell` line: a value that fails validation, or a line
    # that is not valid TOML at all. The targets root's own name holds U+202E too: the
    # path is printed in the error, and neither the validator (it quotes with !r) nor
    # tomllib (it quotes no line text) puts a raw U+202E there, so the path is what makes
    # this fail unless the start-up error print escapes.
    root = tmp_path / "targets‮root"
    folder = root / "testos-1"
    (folder / "prompts").mkdir(parents=True)
    (folder / "prompts" / "prompts.yaml").write_text(
        DEFAULT_PROMPTS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (folder / "target.toml").write_text(
        'id = "testos-1"\n'
        'display_name = "Test OS 1"\n'
        f"{shell_line}\n"
        'package_owner_query = ["owner-of"]\n'
        '[os_release]\nID = "testos"\nVERSION_ID = "1"\n'
        '[sudo]\nimplementation = "test-sudo"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    release = tmp_path / "os-release"
    release.write_text('ID=testos\nVERSION_ID="1"\n', encoding="utf-8")
    code, out, err, started, stub = start(
        monkeypatch, capsys, targets_root=root, release=release
    )
    assert code != 0
    assert not started.repl
    assert "\\u202e" in err
    assert "‮" not in err
    assert "targets\\u202eroot" in err


# (d) proposal/v1 §Conformance: accepted shapes and rejections ---------------------------

LONG = "a" * 2001

REJECTED = {
    "extra-property": '{"kind": "command", "command": "ls", "risk": "read-only", "x": 1}',
    "missing-kind": '{"command": "ls", "risk": "read-only"}',
    "unknown-kind": '{"kind": "explain", "reason": "unclear-request"}',
    "unknown-risk": '{"kind": "command", "command": "ls", "risk": "harmless"}',
    "unknown-reason": '{"kind": "refuse", "reason": "too-busy"}',
    "empty-command": '{"kind": "command", "command": "", "risk": "read-only"}',
    "clarify-with-command": (
        '{"kind": "clarify", "reason": "unclear-request", "command": "ls"}'
    ),
    "refuse-without-reason": '{"kind": "refuse"}',
    "missing-risk": '{"kind": "command", "command": "ls"}',
    "command-with-reason": (
        '{"kind": "command", "command": "ls", "risk": "read-only", '
        '"reason": "unclear-request"}'
    ),
    "over-long-command": json.dumps(
        {"kind": "command", "command": LONG, "risk": "read-only"}
    ),
    "invalid-json": '{"kind": "command", "command": "ls", ',
    "json-array": '[{"kind": "command", "command": "ls", "risk": "read-only"}]',
    "huge-integer": "9" * 4301,
    # Deep nesting makes the JSON decoder raise RecursionError, which must not escape.
    "deep-nesting": "[" * 100000 + "]" * 100000,
    # A repeated key is rejected, not read as last-key-wins (which would be a command).
    "duplicate-key": (
        '{"kind": "refuse", "kind": "command", "command": "ls", "risk": "read-only"}'
    ),
}


@pytest.mark.parametrize("raw", REJECTED.values(), ids=REJECTED.keys())
def test_non_conforming_response_is_rejected_and_the_repl_keeps_running(raw):
    stub, out = session("show disk usage", "@list files", responses=[raw, DF])
    assert out == NO_USABLE + "\n" + DF_SHOWN + "\n"
    assert len(stub.inputs) == 2


def test_lone_surrogate_command_is_rejected_on_a_utf8_stream():
    raw = '{"kind": "command", "command": "\\ud800", "risk": "read-only"}'
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    from nl_shell.backend import RecordingStub

    stub = RecordingStub([raw, DF])
    repl = Repl(
        scripted("show disk usage", "@list files"), out, backend=stub,
        prompts=loaded_prompts(),
    )
    assert repl.run() == 0
    out.flush()
    assert out.buffer.getvalue().decode("utf-8") == NO_USABLE + "\n" + DF_SHOWN + "\n"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (DF, DF_SHOWN),
        (
            json.dumps({"kind": "command", "command": "a" * 2000, "risk": "mutating"}),
            "Proposed command (risk: mutating, 1 line):\n"
            f"  | {'a' * 2000}\n{NOTHING_RUNS_YET}",
        ),
        ('{"kind": "clarify", "reason": "missing-information"}', CLARIFY),
        ('{"kind": "refuse", "reason": "not-a-shell-task"}', REFUSE),
    ],
    ids=["command", "command-at-2000", "clarify", "refuse"],
)
def test_valid_shapes_are_accepted(raw, shown):
    _, out = session("show disk usage", responses=[raw])
    assert out == shown + "\n"


# (e) how a proposal is shown; hidden characters ----------------------------------------


def test_multi_line_command_is_shown_with_every_line_prefixed():
    raw = json.dumps({"kind": "command", "command": "cd /tmp\nls -la", "risk": "read-only"})
    _, out = session("show disk usage", responses=[raw])
    assert out == (
        "Proposed command (risk: read-only, 2 lines):\n"
        "  | cd /tmp\n"
        "  | ls -la\n"
        f"{NOTHING_RUNS_YET}\n"
    )


def test_tab_in_a_command_is_shown_as_is():
    raw = json.dumps({"kind": "command", "command": "printf 'a\tb'", "risk": "read-only"})
    _, out = session("show disk usage", responses=[raw])
    assert out == (
        "Proposed command (risk: read-only, 1 line):\n"
        "  | printf 'a\tb'\n"
        f"{NOTHING_RUNS_YET}\n"
    )


def test_a_command_line_cannot_pose_as_the_proposal_header():
    spoof = "Proposed command (risk: read-only, 1 line):"
    command = f"rm -rf ~\n{spoof}"
    raw = json.dumps({"kind": "command", "command": command, "risk": "destructive"})
    _, out = session("show disk usage", responses=[raw])
    lines = out.splitlines()
    assert spoof not in lines
    assert f"  | {spoof}" in lines
    assert lines[0] == "Proposed command (risk: destructive, 2 lines):"


HIDDEN = {
    "esc": ("\x1b", "\\x1b"),
    "cr": ("\r", "\\x0d"),
    "del": ("\x7f", "\\x7f"),
    "c1-nel": ("\x85", "\\x85"),
    "rlo-u202e": ("‮", "\\u202e"),
    "zwsp-u200b": ("​", "\\u200b"),
    "bom-ufeff": ("﻿", "\\ufeff"),
    "line-separator-u2028": (" ", "\\u2028"),
    "tag-ue0041": ("\U000e0041", "\\U000e0041"),
    # Range boundaries, so narrowing a range in the predicate turns a vector red.
    "c0-first-u0000": ("\x00", "\\x00"),
    "c0-last-u001f": ("\x1f", "\\x1f"),
    "c1-csi-u009b": ("\x9b", "\\x9b"),
    "bidi-alm-u061c": ("؜", "\\u061c"),
    "paragraph-separator-u2029": (" ", "\\u2029"),
    "tag-last-ue007f": ("\U000e007f", "\\U000e007f"),
}


@pytest.mark.parametrize(("char", "escaped"), HIDDEN.values(), ids=HIDDEN.keys())
def test_command_with_a_hidden_character_is_rejected(char, escaped):
    for ensure_ascii in (True, False):
        raw = json.dumps(
            {"kind": "command", "command": f"ls{char} -la", "risk": "read-only"},
            ensure_ascii=ensure_ascii,
        )
        _, out = session("show disk usage", responses=[raw])
        assert out == NO_USABLE + "\n"


@pytest.mark.parametrize(("char", "escaped"), HIDDEN.values(), ids=HIDDEN.keys())
def test_hidden_character_in_a_request_is_escaped_in_context(char, escaped):
    typed = f"show{char}disk usage"
    stub, out = session(typed, ":context", responses=[DF])
    recorded = stub.inputs[0]
    assert recorded[-1][1].endswith(typed)  # sent raw
    assert out == DF_SHOWN + "\n" + expected_context(recorded) + "\n"
    assert out.splitlines()[-1].endswith(f"show{escaped}disk usage")
    assert char not in out


# (f) `:status` lists the loaded prompt files -------------------------------------------


@pytest.mark.parametrize("with_user_file", [False, True], ids=["default-only", "with-user"])
def test_status_lists_the_loaded_prompt_files(
    monkeypatch, capsys, ubuntu_release, with_user_file
):
    import nl_shell

    user = user_prompts_file()
    if with_user_file:
        user.write_text("system: Mine.\n", encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/nl-shell-test-run")
    code, out, err, started, stub = start(
        monkeypatch, capsys, ":status", release=ubuntu_release
    )
    assert (code, err) == (0, "")
    expected = [
        f"nl-shell {nl_shell.__version__}",
        "target: ubuntu-26.04 (Ubuntu 26.04 LTS)",
        "target matches this OS",
        f"prompts: {DEFAULT_PROMPTS}",
    ]
    if with_user_file:
        expected.append(f"prompts: {user}")
    expected += [
        "model endpoint: unix socket /nonexistent/nl-shell-test-run/nl-shell/model.sock",
        "model backend: local model server (chat-completions over the socket)",
    ]
    assert out.splitlines() == expected


# (g) no backend configured -------------------------------------------------------------


@pytest.mark.parametrize("line", ["show disk usage", "@show disk usage"])
def test_without_a_backend_requests_get_the_no_backend_reply(line):
    out = io.StringIO()
    assert Repl(scripted(line), out).run() == 0
    assert out.getvalue() == NO_BACKEND + "\n"


# Every REPL reply is escaped, not only proposals and `:context` -------------------------


def test_every_repl_reply_escapes_hidden_characters():
    # An unknown meta-command echoes what was typed; a raw U+202E or ESC there would
    # reach the terminal unless the REPL escapes every reply it prints.
    out = io.StringIO()
    assert Repl(scripted(":foo‮\x1b[2J"), out).run() == 0
    printed = out.getvalue()
    assert printed.startswith("Unknown meta-command :foo\\u202e\\x1b[2J. Known: :")
    assert printed.endswith(".\n") and printed.count("\n") == 1
    assert "‮" not in printed
    assert "\x1b" not in printed
