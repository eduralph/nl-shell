import io

import pytest

from nl_shell.repl import NOT_YET, Repl


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


def test_end_of_input_exits_cleanly():
    assert run() == (0, "")


@pytest.mark.parametrize("command", [":quit", ":exit", "  :quit  "])
def test_exit_commands_stop_the_loop(command):
    status, out = run(command, "never read")
    assert status == 0
    assert out == ""


def test_blank_lines_are_ignored():
    assert run("", "   ") == (0, "")


def test_ctrl_c_clears_the_line_and_keeps_going():
    status, out = run(KeyboardInterrupt(), "ls")
    assert status == 0
    assert out == NOT_YET + "\n"


def test_input_is_not_run_yet():
    status, out = run("show disk usage")
    assert status == 0
    assert out == NOT_YET + "\n"
