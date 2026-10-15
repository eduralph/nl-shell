import io

import pytest

from nl_shell.repl import Repl

# Every reply pinned as a literal: the module must import on `main`, where the prefix
# dispatch and its reply constants do not exist, so the table can fail rather than not load.
COMMAND = (
    "That looks like a shell command, but nl-shell can't run anything yet: "
    "the bash session arrives in M0.2."
)
NATURAL_LANGUAGE = (
    "That looks like a natural-language request, but nl-shell can't answer it yet: "
    "the model backend arrives in M0.8."
)
ASK = (
    "That is an ask-mode question (?), but nl-shell can't answer it yet: "
    "ask mode arrives in M0.8."
)
PROPOSE = (
    "That is a one-command request (@), but nl-shell can't propose a command yet: "
    "command proposals arrive in M0.8."
)
AGENT = "@@ is reserved for the agent loop, which arrives with M0.6b (#76)."
CONTEXT = ":context is not available yet: it arrives with M0.8 (#16)."
HISTORY = ":history is not available yet: it arrives with M0.9 (#17)."
# `:status` prints a block whose FIRST line is `nl-shell <version>`. Later issues (#92,
# #16, #77) append lines to it, so the table checks only that first line, not the block.
STATUS = "nl-shell "
UNKNOWN_META = (
    "Unknown meta-command :frobnicate. Known: :context, :history, :status, :quit, :exit."
)
UNKNOWN_META_STATUSX = (
    "Unknown meta-command :statusx. Known: :context, :history, :status, :quit, :exit."
)
USAGE_COMMAND = (
    "Usage: \\<command> treats <command> as a shell command, whatever the router would say."
)
USAGE_ASK = "Usage: ?<question> asks a question in ask mode."
USAGE_PROPOSE = "Usage: @<request> asks for one proposed shell command."
USAGE_AGENT = "Usage: @@<goal> hands a goal to the agent loop (arrives with M0.6b, #76)."
USAGE_META = (
    "Usage: :<name> runs a meta-command. "
    "Known: :context, :history, :status, :quit, :exit."
)

REPLY_NAMES = {
    COMMAND: "command",
    NATURAL_LANGUAGE: "natural-language",
    ASK: "ask",
    PROPOSE: "propose",
    AGENT: "agent-reserved",
    CONTEXT: "context",
    HISTORY: "history",
    STATUS: "status",
    UNKNOWN_META: "unknown-meta",
    UNKNOWN_META_STATUSX: "unknown-meta-statusx",
    USAGE_COMMAND: "usage-command",
    USAGE_ASK: "usage-ask",
    USAGE_PROPOSE: "usage-propose",
    USAGE_AGENT: "usage-agent",
    USAGE_META: "usage-meta",
}


def scripted(*lines):
    """A read function that returns ``lines`` in order, then signals end of input."""
    it = iter(lines)

    def read(_prompt):
        try:
            item = next(it)
        except StopIteration:
            raise EOFError from None
        if isinstance(item, BaseException):
            raise item
        return item

    return read


def run(*lines):
    out = io.StringIO()
    status = Repl(scripted(*lines), out).run()
    return status, out.getvalue()


def make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    """``$PATH`` holding only fake ``ls``, ``git`` and ``find``, so routing does not
    depend on what the host has installed. Returns the directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ls", "git", "find"):
        make_executable(bin_dir / name)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_replies_are_distinct():
    assert len(REPLY_NAMES) == 15


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The prefix wins over the router: alone, the router calls this natural language.
        ("\\frobnicate the widgets", COMMAND),
        ("\\ls -la", COMMAND),
        ("?why did that fail", ASK),
        ("@show disk usage", PROPOSE),
        # `@@` is checked before `@`: this must not reach the `@` route as "@clean ...".
        ("@@clean up my downloads", AGENT),
        (":context", CONTEXT),
        (":history", HISTORY),
        (":status", STATUS),
        (":frobnicate", UNKNOWN_META),
        # Names match whole and exact: `:statusx` is unknown, not `:status`.
        (":statusx", UNKNOWN_META_STATUSX),
        # The payload after any prefix is stripped, `:` included.
        (": status", STATUS),
        (":  context", CONTEXT),
        ("? why did that fail", ASK),
        ("@ show disk usage", PROPOSE),
        ("\\ frobnicate the widgets", COMMAND),
        # A bare prefix gets a usage reply and routes nothing.
        ("\\", USAGE_COMMAND),
        ("?", USAGE_ASK),
        ("@", USAGE_PROPOSE),
        ("@@", USAGE_AGENT),
        (":", USAGE_META),
        # Plain input still routes exactly as before.
        ("ls -la", COMMAND),
        ("show disk usage", NATURAL_LANGUAGE),
    ],
    ids=lambda value: REPLY_NAMES.get(value, value),
)
def test_prefix_dispatch(fake_path, line, expected):
    status, out = run(line)
    assert status == 0
    if expected == STATUS:
        import nl_shell

        assert out.splitlines()[0] == STATUS + nl_shell.__version__
    else:
        assert out == expected + "\n"


def test_status_prints_the_version_line(fake_path):
    import nl_shell

    status, out = run(":status")
    assert status == 0
    assert f"nl-shell {nl_shell.__version__}" in out.splitlines()


def test_status_lines_can_be_extended(fake_path):
    """Later issues add `:status` lines by appending a zero-argument callable."""
    import nl_shell
    from nl_shell import input_modes

    def extra():
        return "extra: yes"

    input_modes.STATUS_LINES.append(extra)
    try:
        status, out = run(":status")
    finally:
        input_modes.STATUS_LINES.remove(extra)
    assert status == 0
    lines = out.splitlines()
    assert lines.index("extra: yes") > lines.index(STATUS + nl_shell.__version__)


# The exit commands are meta-commands: the payload after `:` is stripped for them too, so
# `: quit` exits rather than being called an unknown `:quit` while `:quit` is listed known.
@pytest.mark.parametrize("command", [":quit", ":exit", ": quit", ":  exit"])
def test_exit_commands_still_stop_the_loop(fake_path, command):
    status, out = run(command, ":status")
    assert status == 0
    assert out == ""
