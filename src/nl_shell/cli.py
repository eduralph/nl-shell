"""Command-line entry point."""

from __future__ import annotations

import argparse

from nl_shell import __version__
from nl_shell.repl import Repl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nl-shell",
        description="Natural-language shell for Ubuntu 26.04.",
    )
    parser.add_argument("--version", action="version", version=f"nl-shell {__version__}")
    parser.parse_args(argv)
    return Repl.interactive().run()
