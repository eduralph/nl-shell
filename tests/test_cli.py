import pytest

from nl_shell import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"nl-shell {__version__}"


def test_main_runs_the_interactive_repl(monkeypatch):
    class FakeRepl:
        def run(self):
            return 0

    monkeypatch.setattr(cli.Repl, "interactive", classmethod(lambda cls: FakeRepl()))
    assert cli.main([]) == 0
