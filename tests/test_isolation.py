import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import GUARD_EXIT, GUARDED


def test_home_and_history_are_temporary(tmp_path_factory):
    home = Path(os.environ["HOME"])
    assert home.is_relative_to(tmp_path_factory.getbasetemp())
    assert Path(os.environ["HISTFILE"]) == home / ".bash_history"
    assert Path(os.path.expanduser("~")) == home


def test_xdg_directories_are_inside_the_temporary_home():
    home = Path(os.environ["HOME"])
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(os.environ[var]).is_relative_to(home)
        assert Path(os.environ[var]).is_dir()


def test_bash_history_lands_in_the_temporary_home():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    subprocess.run(["bash", "-ic", "history -s 'echo marker'; history -w"],
                   check=True, capture_output=True, env=os.environ)
    assert "echo marker" in (Path(os.environ["HOME"]) / ".bash_history").read_text()


@pytest.mark.parametrize("name", GUARDED)
def test_privilege_commands_hit_the_guard(name):
    found = shutil.which(name)
    assert found is not None
    result = subprocess.run([name, "true"], capture_output=True, text=True)
    assert result.returncode == GUARD_EXIT
    assert "blocked outside the reference VM" in result.stderr


@pytest.mark.parametrize("marker,env", [("sudo", "NL_SHELL_TEST_VM"), ("vm", "NL_SHELL_TEST_VM"),
                                        ("gpu", "NL_SHELL_TEST_GPU"), ("teacher", "NL_SHELL_TEST_TEACHER")])
def test_environment_markers_skip_unless_enabled(pytester, monkeypatch, marker, env):
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(f"""
        import pytest

        @pytest.mark.{marker}
        def test_needs_env():
            pass
    """)
    pytester.makeini(f"[pytest]\nmarkers =\n    {marker}: x\n")

    monkeypatch.delenv(env, raising=False)
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(skipped=1)

    monkeypatch.setenv(env, "1")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)
