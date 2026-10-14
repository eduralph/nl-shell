"""Test isolation for the whole suite (#73).

Every test runs with a temporary HOME, history file and XDG directories, and with guard
scripts ahead of the real `sudo`, `su`, `pkexec` and `doas` on PATH. A test can therefore
never write the developer's ~/.bash_history or reach their live sudo — whether it runs on
a laptop, in CI, or inside a PDCA gate.

Tests that genuinely need a real environment declare it with a marker and only run when
the environment variable says that environment is available:

    marker   runs only when             typical use
    sudo     NL_SHELL_TEST_VM=1         real sudo-rs, in the throwaway reference VM
    vm       NL_SHELL_TEST_VM=1         anything else that must run in the reference VM
    gpu      NL_SHELL_TEST_GPU=1        model inference or training on a GPU
    teacher  NL_SHELL_TEST_TEACHER=1    calls to the teacher endpoint

A `sudo`-marked test also gets the real PATH back, since it is in the VM by definition.
"""

from __future__ import annotations

import os
import stat

import pytest

pytest_plugins = ["pytester"]

ENV_FOR_MARKER = {
    "sudo": "NL_SHELL_TEST_VM",
    "vm": "NL_SHELL_TEST_VM",
    "gpu": "NL_SHELL_TEST_GPU",
    "teacher": "NL_SHELL_TEST_TEACHER",
}

GUARDED = ("sudo", "su", "pkexec", "doas")
GUARD_EXIT = 99
GUARD_SCRIPT = f"""#!/bin/sh
echo "nl-shell tests: real $(basename "$0") is blocked outside the reference VM (#73)" >&2
exit {GUARD_EXIT}
"""


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, env in ENV_FOR_MARKER.items():
            if item.get_closest_marker(marker) and os.environ.get(env) != "1":
                item.add_marker(pytest.mark.skip(reason=f"needs {env}=1 ({marker} test)"))


@pytest.fixture(autouse=True)
def isolated_environment(request, tmp_path_factory, monkeypatch):
    """A temporary HOME and history for every test; guarded privilege commands."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HISTFILE", str(home / ".bash_history"))
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                     ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        path = home / sub
        path.mkdir(parents=True)
        monkeypatch.setenv(var, str(path))

    if request.node.get_closest_marker("sudo"):
        return  # runs only in the reference VM (see module docstring)
    guard_dir = tmp_path_factory.mktemp("guard-bin")
    for name in GUARDED:
        script = guard_dir / name
        script.write_text(GUARD_SCRIPT, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{guard_dir}{os.pathsep}{os.environ.get('PATH', '')}")
