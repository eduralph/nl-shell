import io

import pytest

from nl_shell.repl import Repl

# The two route replies, pinned as literals: the module must import on `main`, where
# these constants do not exist, so the router-table test can fail rather than not load.
COMMAND = (
    "That looks like a shell command, but nl-shell can't run anything yet: "
    "the bash session arrives in M0.2."
)
# A plain Repl has no model backend (the interactive default until #77).
NATURAL_LANGUAGE = (
    "No model backend is configured, so nl-shell can't answer this yet: "
    "the model client arrives with #77."
)
ROUTE_NAMES = {COMMAND: "command", NATURAL_LANGUAGE: "natural-language"}


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
    """``$PATH`` holding only fake ``ls``, ``git``, ``find``, ``watch``, ``who``,
    ``free``, ``sort`` and ``grep``, so routing does not depend on what the host has
    installed. Returns the directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ls", "git", "find", "watch", "who", "free", "sort", "grep"):
        make_executable(bin_dir / name)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_end_of_input_exits_cleanly():
    assert run() == (0, "")


@pytest.mark.parametrize("command", [":quit", ":exit", "  :quit  "])
def test_exit_commands_stop_the_loop(command):
    status, out = run(command, "never read")
    assert status == 0
    assert out == ""


def test_blank_lines_are_ignored():
    assert run("", "   ") == (0, "")


def test_ctrl_c_clears_the_line_and_keeps_going(fake_path):
    status, out = run(KeyboardInterrupt(), "ls")
    assert status == 0
    assert out == COMMAND + "\n"


def test_input_is_not_run_yet(fake_path):
    status, out = run("show disk usage")
    assert status == 0
    assert out == NATURAL_LANGUAGE + "\n"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ls -la", COMMAND),
        ("git status", COMMAND),
        ("cd /tmp", COMMAND),  # bash builtin, not on $PATH
        ("export FOO=1", COMMAND),  # bash builtin, not on $PATH
        ("  ls", COMMAND),
        ("show disk usage", NATURAL_LANGUAGE),
        ("what's using port 80", NATURAL_LANGUAGE),  # unbalanced quote must not raise
        ("frobnicate the widgets", NATURAL_LANGUAGE),
        # English that starts with a command name (the English-sentence rule).
        ("find large files in my home", NATURAL_LANGUAGE),  # function words
        ("find what's using port 80", NATURAL_LANGUAGE),  # unbalanced quote
        ("watch the logs for errors", NATURAL_LANGUAGE),  # function words
        ("who is logged in", NATURAL_LANGUAGE),  # 4 words, "is", "in"
        ("kill the process on port 8080", NATURAL_LANGUAGE),  # builtin, function words
        ("find . -name '*.py'", COMMAND),  # option, quotes, no function word
        ("ls my files", COMMAND),  # only 3 words
        ("free -h", COMMAND),  # option
        ("echo all is well in the world", COMMAND),  # echo's operands are free text
        ("sort users.txt", COMMAND),  # only 2 words
        ("echo it's fine", NATURAL_LANGUAGE),  # quote signal beats the echo rule
        ('git commit -m "wip', COMMAND),  # an unclosed " is not the quote signal
        # Each line below reads as English on every count but one, so each pins a
        # single part of the rule.
        ("printf the answer is 42", COMMAND),  # printf's operands are free text
        ("find the big files -size +1G", COMMAND),  # a word starts with "-"
        ("find big files in ~/src", COMMAND),  # metacharacters "~" and "/"
        ("grep the in file|less", COMMAND),  # metacharacter "|"
        ('grep "the files in here', COMMAND),  # metacharacter '"'
        ("git push origin main", COMMAND),  # 4 plain words, no function word
        ("grep it\\'s x y", COMMAND),  # a "\" turns the quote signal off
        ("git commit -m \"it's\"", COMMAND),  # a '"' turns the quote signal off
        # The quote signal is checked before the "-" and metacharacter rule, so it wins.
        ("find . -name it's", NATURAL_LANGUAGE),  # beats the "-" word rule
        ("find what's in ~/src", NATURAL_LANGUAGE),  # beats the metacharacter rule
    ],
    ids=lambda value: ROUTE_NAMES.get(value, value),
)
def test_input_is_routed_as_command_or_natural_language(fake_path, line, expected):
    status, out = run(line)
    assert status == 0
    assert out == expected + "\n"


def test_path_to_an_executable_routes_as_a_command(fake_path, tmp_path):
    script = make_executable(tmp_path / "build.sh")
    status, out = run(f"{script} --release")
    assert status == 0
    assert out == COMMAND + "\n"


@pytest.mark.parametrize("kind", ["non-executable file", "directory", "no such path"])
def test_path_that_is_not_an_executable_routes_as_natural_language(
    fake_path, tmp_path, monkeypatch, kind
):
    """A first word containing ``/`` is a command only if it names an executable file."""
    monkeypatch.chdir(tmp_path)
    if kind == "non-executable file":
        (tmp_path / "notes.txt").write_text("hello\n")
        line = "./notes.txt says what"
    elif kind == "directory":
        (tmp_path / "docs").mkdir()
        line = f"{tmp_path / 'docs'} is where the guides live"
    else:
        line = "w/o sudo how do I install this"
    status, out = run(line)
    assert status == 0
    assert out == NATURAL_LANGUAGE + "\n"


def test_path_is_read_on_every_line(fake_path, tmp_path, monkeypatch):
    """``$PATH`` is looked up per line, not cached: a tool added later is found."""
    later = tmp_path / "later"
    later.mkdir()
    make_executable(later / "frobnicate")
    calls = []

    def read(_prompt):
        calls.append(None)
        if len(calls) > 2:
            raise EOFError
        if len(calls) == 2:
            monkeypatch.setenv("PATH", f"{fake_path}:{later}")
        return "frobnicate the widgets"

    out = io.StringIO()
    assert Repl(read, out).run() == 0
    assert out.getvalue() == NATURAL_LANGUAGE + "\n" + COMMAND + "\n"


def test_exit_commands_are_checked_before_routing(fake_path):
    status, out = run("ls", ":quit", "git status")
    assert status == 0
    assert out == COMMAND + "\n"
