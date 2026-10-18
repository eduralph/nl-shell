"""OS targets: target.toml loading, os-release reading and start-up selection (#92).

Every case uses a fake os-release file and a fake targets directory under ``tmp_path``, so
the result never depends on the host OS. Start-up is driven through the real ``cli.main``,
with ``Repl.interactive`` replaced by a ``Repl`` reading scripted lines (as
``tests/test_repl.py`` does). The modules this issue adds are imported inside the tests,
never at module level, so the file still loads (and fails) on a tree without them.
"""

import io
import os
import re
import tomllib
from pathlib import Path

import pytest

import nl_shell
from nl_shell import cli
from nl_shell.repl import Repl

REPO = Path(__file__).resolve().parents[1]
VERSION_LINE = f"nl-shell {nl_shell.__version__}"
MISMATCH = "target does not match this OS"

UBUNTU_2604 = """\
id = "ubuntu-26.04"
display_name = "Ubuntu 26.04 LTS"
shell = "bash"
package_owner_query = ["dpkg", "-S"]

[os_release]
ID = "ubuntu"
VERSION_ID = "26.04"

[sudo]
implementation = "sudo-rs"
version = "0.2.13"
"""
TESTOS_1 = """\
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

# Every target folder carries its default prompts layer (#143); start-up refuses one without.
PROMPTS_YAML = """\
system: You propose one bash command.
examples: []
request_template: "{state}{request}"
"""

UBUNTU_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="26.04"\n'
DEBIAN_RELEASE = 'NAME="Debian GNU/Linux"\nID=debian\nID_LIKE=ubuntu\nVERSION_ID="13"\n'


def scripted(*lines):
    """A read function that returns ``lines`` in order, then signals end of input."""
    it = iter(lines)

    def read(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


class Fake:
    """A fake targets root and os-release files under one tmp_path."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "targets"
        self.root.mkdir()
        self.etc = tmp_path / "etc" / "os-release"
        self.usr_lib = tmp_path / "usr-lib" / "os-release"
        self.etc.parent.mkdir()
        self.usr_lib.parent.mkdir()
        self.add_target("ubuntu-26.04", UBUNTU_2604)
        self.add_target("testos-1", TESTOS_1)

    @property
    def paths(self):
        return [self.etc, self.usr_lib]

    def add_target(self, folder, text):
        (self.root / folder).mkdir(exist_ok=True)
        prompts = self.root / folder / "prompts"
        prompts.mkdir(exist_ok=True)
        (prompts / "prompts.yaml").write_text(PROMPTS_YAML, encoding="utf-8")
        path = self.root / folder / "target.toml"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        return path

    def release(self, text):
        self.etc.write_text(text, encoding="utf-8")


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path)


@pytest.fixture
def config_file():
    """The config.toml path under the suite's temporary XDG_CONFIG_HOME (conftest, #73)."""
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "nl-shell" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class Run:
    def __init__(self, code, out, err, started, selection):
        self.code = code
        self.out = out
        self.err = err
        self.started = started
        self.selection = selection


@pytest.fixture
def start(fake, monkeypatch, capsys):
    """Run the real ``cli.main`` against ``fake``; the REPL reads ``lines`` then ends.

    Also records the Selection ``cli.main`` got from ``targets.select``, so tests assert
    the result #12 will read directly, not only through printed text.
    """

    def run(argv, *lines):
        from nl_shell import targets

        out = io.StringIO()
        started = []
        selections = []
        real_select = targets.select

        def spy(*args, **kwargs):
            selection = real_select(*args, **kwargs)
            selections.append(selection)
            return selection

        def interactive(cls, status_lines=(), **kwargs):
            started.append(True)
            return Repl(scripted(*lines), out, status_lines, **kwargs)

        monkeypatch.setattr(targets, "select", spy)
        monkeypatch.setattr(Repl, "interactive", classmethod(interactive))
        try:
            code = cli.main(argv, targets_root=fake.root, os_release_paths=fake.paths)
        finally:
            monkeypatch.setattr(targets, "select", real_select)
        err = capsys.readouterr().err
        assert "Traceback" not in err
        selection = selections[-1] if selections else None
        return Run(code, out.getvalue(), err, bool(started), selection)

    return run


def prompts_line(fake, target_id):
    return f"prompts: {fake.root / target_id / 'prompts' / 'prompts.yaml'}"


def no_match_message(detected, config_file, available="testos-1, ubuntu-26.04"):
    return (
        f"nl-shell: no target matches this OS (detected: {detected}).\n"
        f"Available targets: {available}.\n"
        f'Name one with --target <id>, or with target = "<id>" in {config_file}.\n'
    )


# (a) exact match ------------------------------------------------------------------------


def test_exact_match_is_active_and_matching(fake, start):
    fake.release(UBUNTU_RELEASE)
    run = start([], ":status")
    assert run.code == 0
    assert run.started
    assert run.err == ""
    assert run.out == (
        f"{VERSION_LINE}\n"
        "target: ubuntu-26.04 (Ubuntu 26.04 LTS)\n"
        "target matches this OS\n"
        f"{prompts_line(fake, 'ubuntu-26.04')}\n"
    )
    sel = run.selection
    assert sel.target.id == "ubuntu-26.04"
    assert sel.target.folder == fake.root / "ubuntu-26.04"
    assert sel.matches is True
    assert sel.reason is None
    assert (sel.detected_id, sel.detected_version_id) == ("ubuntu", "26.04")


def test_named_target_that_matches_is_marked_matching(fake, start):
    fake.release(UBUNTU_RELEASE)
    run = start(["--target", "ubuntu-26.04"], ":status")
    assert run.code == 0
    assert run.err == ""
    assert run.selection.matches is True
    assert run.selection.reason is None


# (b) no exact match, nothing named ------------------------------------------------------


@pytest.mark.parametrize(
    ("release", "detected"),
    [
        ('ID=ubuntu\nVERSION_ID="26.10"\n', "ubuntu 26.10"),  # no near-version fallback
        (DEBIAN_RELEASE, "debian 13"),  # no ID_LIKE fallback
    ],
    ids=["near-version", "id-like"],
)
def test_no_match_and_no_named_target_is_refused(
    fake, start, config_file, release, detected
):
    fake.release(release)
    run = start([], ":status")
    assert run.code != 0
    assert not run.started
    assert run.out == ""
    assert run.err == no_match_message(detected, config_file)


# (c) named target on a non-matching OS --------------------------------------------------


def test_named_target_on_another_os_starts_marked_not_matching(fake, start):
    fake.release(DEBIAN_RELEASE)
    run = start(["--target", "ubuntu-26.04"], ":status")
    assert run.code == 0
    assert run.started
    assert run.err == (
        "nl-shell: warning: target ubuntu-26.04 does not match this OS "
        "(detected: debian 13).\n"
    )
    assert run.out == (
        f"{VERSION_LINE}\n"
        "target: ubuntu-26.04 (Ubuntu 26.04 LTS)\n"
        "target does not match this OS (detected: debian 13)\n"
        f"{prompts_line(fake, 'ubuntu-26.04')}\n"
    )
    sel = run.selection
    assert sel.target.id == "ubuntu-26.04"
    assert sel.matches is False
    assert sel.reason == "target does not match this OS"
    assert sel.detected_id == "debian"
    assert sel.detected_version_id == "13"


def test_mismatch_reason_is_the_spec_text():
    from nl_shell import targets

    # spec policy-gate/v1 §7.7
    assert targets.MISMATCH_REASON == MISMATCH


# (d) config.toml ------------------------------------------------------------------------


def test_config_target_behaves_like_the_flag(fake, start, config_file):
    fake.release(DEBIAN_RELEASE)
    config_file.write_text('target = "ubuntu-26.04"\n', encoding="utf-8")
    run = start([], ":status")
    assert run.code == 0
    assert run.started
    assert run.err == (
        "nl-shell: warning: target ubuntu-26.04 does not match this OS "
        "(detected: debian 13).\n"
    )
    assert "target does not match this OS (detected: debian 13)" in run.out.splitlines()
    assert run.selection.target.id == "ubuntu-26.04"
    assert run.selection.matches is False


def test_flag_wins_over_config(fake, start, config_file):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text('target = "testos-1"\n', encoding="utf-8")
    run = start(["--target", "ubuntu-26.04"], ":status")
    assert run.code == 0
    assert run.selection.target.id == "ubuntu-26.04"
    assert run.selection.matches is True
    assert "target: ubuntu-26.04 (Ubuntu 26.04 LTS)" in run.out.splitlines()
    assert "testos-1" not in run.out


def test_flag_wins_over_a_config_target_that_does_not_exist(fake, start, config_file):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text('target = "nosuch-9"\n', encoding="utf-8")
    run = start(["--target", "ubuntu-26.04"])
    assert run.code == 0
    assert run.selection.target.id == "ubuntu-26.04"


def test_config_that_is_not_valid_toml_is_refused(fake, start, config_file):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text("target = \n", encoding="utf-8")
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err.startswith(f"nl-shell: {config_file}: not valid TOML: ")


@pytest.mark.parametrize("value", ["5", "true", '["ubuntu-26.04"]', '{ id = "x" }'])
def test_config_target_that_is_not_a_string_is_refused(fake, start, config_file, value):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text(f"target = {value}\n", encoding="utf-8")
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err == f"nl-shell: {config_file}: `target` must be a string\n"


def test_config_unknown_keys_are_ignored(fake, start, config_file):
    fake.release(DEBIAN_RELEASE)
    config_file.write_text(
        'frobnicate = 1\ntarget = "testos-1"\n[future]\nanything = "x"\n', encoding="utf-8"
    )
    run = start([], ":status")
    assert run.code == 0
    assert run.selection.target.id == "testos-1"


def test_config_without_target_selects_by_os_release(fake, start, config_file):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text("other = 1\n", encoding="utf-8")
    run = start([])
    assert run.code == 0
    assert run.selection.target.id == "ubuntu-26.04"


@pytest.mark.parametrize("xdg", ["", "relative/config"])
def test_relative_or_empty_xdg_config_home_is_ignored(
    fake, start, tmp_path, monkeypatch, xdg
):
    home = Path(os.environ["HOME"])
    (home / ".config" / "nl-shell").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "nl-shell" / "config.toml").write_text(
        'target = "testos-1"\n', encoding="utf-8"
    )
    # A config.toml the relative value would find, if it were (wrongly) honoured.
    workdir = tmp_path / "cwd"
    (workdir / "relative" / "config" / "nl-shell").mkdir(parents=True)
    (workdir / "relative" / "config" / "nl-shell" / "config.toml").write_text(
        'target = "ubuntu-26.04"\n', encoding="utf-8"
    )
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    fake.release(DEBIAN_RELEASE)
    run = start([])
    assert run.code == 0
    assert run.selection.target.id == "testos-1"


def test_config_path_follows_the_xdg_base_directory_spec():
    from nl_shell import config

    env = {"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg"}
    assert config.config_path(env) == Path("/xdg/nl-shell/config.toml")
    for xdg in ("", "rel", "./rel"):
        env = {"HOME": "/home/u", "XDG_CONFIG_HOME": xdg}
        assert config.config_path(env) == Path("/home/u/.config/nl-shell/config.toml")
    default = Path("/home/u/.config/nl-shell/config.toml")
    assert config.config_path({"HOME": "/home/u"}) == default


# (e) named target with no folder, or not a plain folder name ----------------------------


def test_named_target_with_no_folder_is_refused(fake, start):
    fake.release(UBUNTU_RELEASE)
    run = start(["--target", "nosuch-1"])
    assert run.code != 0
    assert not run.started
    assert run.err == (
        f"nl-shell: no target 'nosuch-1' in {fake.root} "
        "(available: testos-1, ubuntu-26.04)\n"
    )


@pytest.mark.parametrize("target_id", ["../x", "", "a/b", "/x", "..", ".", "x/../x"])
def test_named_target_that_is_not_a_plain_folder_name_is_refused(
    fake, start, config_file, target_id
):
    # A valid target just outside the root, which `../x` would reach if it were joined.
    outside = fake.tmp / "x"
    outside.mkdir()
    outside_toml = TESTOS_1.replace("testos-1", "x")
    (outside / "target.toml").write_text(outside_toml, encoding="utf-8")
    (fake.root / "a").mkdir()
    fake.release(UBUNTU_RELEASE)
    run = start(["--target", target_id])
    assert run.code != 0
    assert not run.started
    assert run.err == (
        f"nl-shell: invalid target id {target_id!r}: a target id is a plain folder name "
        "(letters, digits, '.', '_' and '-'; no '/')\n"
    )


def test_invalid_id_in_config_is_refused(fake, start, config_file):
    fake.release(UBUNTU_RELEASE)
    config_file.write_text('target = "../x"\n', encoding="utf-8")
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err.startswith("nl-shell: invalid target id '../x'")


def test_invalid_id_is_refused_before_any_path_is_built(fake, monkeypatch):
    """The id check runs before ``root / id``: no path is joined for a bad id."""
    from nl_shell import targets

    joined = []

    class Root(type(fake.root)):
        def __truediv__(self, other):
            joined.append(other)
            return super().__truediv__(other)

    root = Root(fake.root)
    for bad in ("../x", "", "a/b"):
        with pytest.raises(targets.TargetError):
            targets.load(bad, root)
    assert joined == []


# (f) broken target.toml -----------------------------------------------------------------

BROKEN = "broken-1"
GOOD_BROKEN = TESTOS_1.replace('id = "testos-1"', f'id = "{BROKEN}"')


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (None, "missing"),
        (GOOD_BROKEN.replace('version = "1.0"', "version = "), "not valid TOML: "),
        (GOOD_BROKEN.replace('display_name = "Test OS 1"\n', ""),
         "missing required field `display_name`"),
        (GOOD_BROKEN.replace(f'id = "{BROKEN}"\n', ""), "missing required field `id`"),
        (GOOD_BROKEN.replace('shell = "bash"\n', ""), "missing required field `shell`"),
        (GOOD_BROKEN.replace('package_owner_query = ["owner-of"]\n', ""),
         "missing required field `package_owner_query`"),
        (GOOD_BROKEN.replace('VERSION_ID = "1"\n', ""),
         "missing required field `os_release.VERSION_ID`"),
        (GOOD_BROKEN.replace('version = "1.0"\n', ""),
         "missing required field `sudo.version`"),
        (GOOD_BROKEN.split("[sudo]")[0], "missing required table `[sudo]`"),
        (GOOD_BROKEN.replace(f'id = "{BROKEN}"', 'id = "other-1"'),
         "id 'other-1' does not match its folder name 'broken-1'"),
        (GOOD_BROKEN.replace('shell = "bash"', 'shell = "zsh"'),
         "shell 'zsh' is not supported (supported: bash)"),
    ],
    ids=["no-file", "not-toml", "no-display-name", "no-id", "no-shell", "no-query",
         "no-version-id", "no-sudo-version", "no-sudo-table", "id-not-folder", "not-bash"],
)
def test_broken_target_toml_is_refused(fake, start, text, problem):
    path = fake.add_target(BROKEN, text)
    fake.release(UBUNTU_RELEASE)
    run = start(["--target", BROKEN])
    assert run.code != 0
    assert not run.started
    assert run.err.startswith(f"nl-shell: {path}: {problem}")
    assert run.err.endswith("\n") and run.err.count("\n") == 1


def test_broken_target_toml_of_the_matching_folder_is_refused(fake, start):
    broken = UBUNTU_2604.replace('shell = "bash"', 'shell = "sh"')
    path = fake.add_target("ubuntu-26.04", broken)
    fake.release(UBUNTU_RELEASE)
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err == f"nl-shell: {path}: shell 'sh' is not supported (supported: bash)\n"


def test_folder_named_like_the_os_with_disagreeing_match_is_not_selected(
    fake, start, config_file
):
    disagreeing = UBUNTU_2604.replace('VERSION_ID = "26.04"', 'VERSION_ID = "26.10"')
    fake.add_target("ubuntu-26.04", disagreeing)
    fake.release(UBUNTU_RELEASE)
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err == no_match_message("ubuntu 26.04", config_file)


# (g) os-release handling ----------------------------------------------------------------


def test_usr_lib_os_release_is_read_when_etc_is_missing(fake, start):
    fake.usr_lib.write_text(UBUNTU_RELEASE, encoding="utf-8")
    assert not fake.etc.exists()
    run = start([])
    assert run.code == 0
    assert run.selection.target.id == "ubuntu-26.04"
    assert run.selection.matches is True


def make_unreadable(fake, how):
    if how == "directory":
        fake.etc.mkdir()
    elif how == "permission-denied":
        if os.geteuid() == 0:
            pytest.skip("root can read a mode-000 file; the directory case covers this")
        fake.etc.write_text(UBUNTU_RELEASE, encoding="utf-8")
        fake.etc.chmod(0)
    else:  # not valid UTF-8
        fake.etc.write_bytes(b'ID=ubuntu\nVERSION_ID="26.04"\nNAME="\xff"\n')
    # The fallback would match; it must not be used.
    fake.usr_lib.write_text(UBUNTU_RELEASE, encoding="utf-8")


UNREADABLE = ["directory", "permission-denied", "not-utf-8"]


@pytest.mark.parametrize("how", UNREADABLE)
def test_unreadable_etc_os_release_without_named_target_is_refused(
    fake, start, config_file, how
):
    make_unreadable(fake, how)
    run = start([])
    assert run.code != 0
    assert not run.started
    first, *rest = run.err.splitlines()
    prefix = f"nl-shell: cannot tell which OS this is: cannot read {fake.etc}: "
    assert first.startswith(prefix)
    assert rest == [
        "Available targets: testos-1, ubuntu-26.04.",
        f'Name one with --target <id>, or with target = "<id>" in {config_file}.',
    ]


@pytest.mark.parametrize("how", UNREADABLE)
def test_unreadable_etc_os_release_with_named_target_starts_not_matching(fake, start, how):
    make_unreadable(fake, how)
    run = start(["--target", "ubuntu-26.04"], ":status")
    assert run.code == 0
    assert run.started
    assert run.err == (
        "nl-shell: warning: target ubuntu-26.04 does not match this OS "
        "(detected: unknown unknown).\n"
    )
    assert run.out == (
        f"{VERSION_LINE}\n"
        "target: ubuntu-26.04 (Ubuntu 26.04 LTS)\n"
        "target does not match this OS (detected: unknown unknown)\n"
        f"{prompts_line(fake, 'ubuntu-26.04')}\n"
    )
    sel = run.selection
    assert sel.matches is False
    assert sel.reason == MISMATCH
    assert (sel.detected_id, sel.detected_version_id) == ("unknown", "unknown")


def test_no_os_release_at_all_is_refused(fake, start):
    run = start([])
    assert run.code != 0
    assert not run.started
    assert run.err.startswith(
        "nl-shell: cannot tell which OS this is: no os-release file found "
        f"(looked for {fake.etc}, {fake.usr_lib}).\n"
    )


def test_os_release_quoting_is_parsed_as_os_release_5_describes():
    from nl_shell import os_release

    text = (
        "# a comment\n"
        "\n"
        "ID=ubuntu\n"
        'VERSION_ID="26.04"\n'
        "NAME='Ubuntu Linux'\n"
        'PRETTY_NAME="say \\"hi\\" \\$HOME \\\\ \\`x\\` it\'s"\n'
        "SINGLE='no \\\"escapes\\\" here'\n"
        "EMPTY=\n"
        'EMPTY_QUOTED=""\n'
        "BAD=two words\n"
        'UNBALANCED="open\n'
        "  INDENTED=yes  \n"
    )
    assert os_release.parse(text) == {
        "ID": "ubuntu",
        "VERSION_ID": "26.04",
        "NAME": "Ubuntu Linux",
        "PRETTY_NAME": "say \"hi\" $HOME \\ `x` it's",
        "SINGLE": 'no \\"escapes\\" here',
        "EMPTY": "",
        "EMPTY_QUOTED": "",
        "INDENTED": "yes",
    }


@pytest.mark.parametrize(
    "release",
    ["ID='ubuntu'\nVERSION_ID='26.04'\n", 'ID="ubuntu"\nVERSION_ID=26.04\n'],
    ids=["single-quoted", "double-and-unquoted"],
)
def test_quoted_os_release_values_match(fake, start, release):
    fake.release(release)
    run = start([])
    assert run.code == 0
    assert run.selection.matches is True


# (h) no allowlist.toml needed -----------------------------------------------------------


def test_target_folder_without_allowlist_loads(fake):
    from nl_shell import targets

    folder = fake.root / "testos-1"
    assert sorted(p.name for p in folder.iterdir()) == ["prompts", "target.toml"]
    target = targets.load("testos-1", fake.root)
    assert target.id == "testos-1"
    assert target.folder == folder


# (i) the real ubuntu-26.04 target -------------------------------------------------------


def test_real_ubuntu_target_loads_and_matches():
    from nl_shell import targets

    root = REPO / "targets"
    assert targets.TARGETS_ROOT == root
    target = targets.load("ubuntu-26.04", root)
    assert target.id == "ubuntu-26.04"
    assert target.folder == root / "ubuntu-26.04"
    assert target.matches({"ID": "ubuntu", "VERSION_ID": "26.04"})
    assert not target.matches({"ID": "ubuntu", "VERSION_ID": "26.10"})
    assert target.shell == "bash"
    assert " ".join(target.package_owner_query) == "dpkg -S"
    assert target.sudo_implementation == "sudo-rs"
    assert target.sudo_version == "0.2.13"
    assert target.display_name


def test_real_ubuntu_target_is_selected_on_a_matching_os(tmp_path):
    from nl_shell import targets

    release = tmp_path / "os-release"
    release.write_text(UBUNTU_RELEASE, encoding="utf-8")
    sel = targets.select(None, REPO / "targets", [release])
    assert sel.target.id == "ubuntu-26.04"
    assert sel.matches is True


# (j) no OS name in code -----------------------------------------------------------------

OS_NAME = re.compile(r"ubuntu|26\.04", re.IGNORECASE)


def test_no_os_name_in_package_code():
    sources = sorted((REPO / "src" / "nl_shell").rglob("*.py"))
    assert sources
    hits = [
        f"{path.relative_to(REPO)}:{number}: {line.strip()}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if OS_NAME.search(line)
    ]
    assert hits == []


def test_no_os_name_in_project_description():
    with open(REPO / "pyproject.toml", "rb") as f:
        description = tomllib.load(f)["project"]["description"]
    assert not OS_NAME.search(description)


# (k) :status lines are per session ------------------------------------------------------


def test_repl_without_target_has_no_target_lines():
    out = io.StringIO()
    assert Repl(scripted(":status"), out).run() == 0
    assert out.getvalue() == f"{VERSION_LINE}\n"


def test_status_lines_do_not_leak_between_runs(fake, start):
    fake.release(UBUNTU_RELEASE)
    first = start([], ":status")
    second = start(["--target", "testos-1"], ":status")
    assert first.out == (
        f"{VERSION_LINE}\n"
        "target: ubuntu-26.04 (Ubuntu 26.04 LTS)\n"
        "target matches this OS\n"
        f"{prompts_line(fake, 'ubuntu-26.04')}\n"
    )
    assert second.out == (
        f"{VERSION_LINE}\n"
        "target: testos-1 (Test OS 1)\n"
        "target does not match this OS (detected: ubuntu 26.04)\n"
        f"{prompts_line(fake, 'testos-1')}\n"
    )
    # And a Repl built afterwards, with no target, shows neither.
    out = io.StringIO()
    assert Repl(scripted(":status"), out).run() == 0
    assert out.getvalue() == f"{VERSION_LINE}\n"
