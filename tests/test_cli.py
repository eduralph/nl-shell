import pytest

from nl_shell import __version__, cli

# A matching os-release and targets root, so `cli.main` does not depend on the host OS.
TARGET_TOML = """\
id = "testos-1"
display_name = "Test OS 1"
shell = "bash"
package_owner_query = ["owner-of"]

[os_release]
ID = "testos"
VERSION_ID = "1"

[sudo]
implementation = "test-sudo"
version = "1.0"
"""


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"nl-shell {__version__}"


def test_main_runs_the_interactive_repl(monkeypatch, tmp_path):
    root = tmp_path / "targets"
    (root / "testos-1").mkdir(parents=True)
    (root / "testos-1" / "target.toml").write_text(TARGET_TOML, encoding="utf-8")
    release = tmp_path / "os-release"
    release.write_text('ID=testos\nVERSION_ID="1"\n', encoding="utf-8")
    seen = {}

    class FakeRepl:
        def run(self):
            return 0

    def interactive(cls, status_lines=()):
        seen["status_lines"] = [line() for line in status_lines]
        return FakeRepl()

    monkeypatch.setattr(cli.Repl, "interactive", classmethod(interactive))
    assert cli.main([], targets_root=root, os_release_paths=[release]) == 0
    assert seen["status_lines"] == [
        "target: testos-1 (Test OS 1)",
        "target matches this OS",
    ]
