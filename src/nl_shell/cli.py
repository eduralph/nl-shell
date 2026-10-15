"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from nl_shell import __version__, config, os_release, targets
from nl_shell.input_modes import StatusLine
from nl_shell.repl import Repl

DESCRIPTION = (
    "Natural-language shell: a small local model proposes commands, bash runs them."
)


def main(
    argv: list[str] | None = None,
    *,
    targets_root: Path | None = None,
    os_release_paths: Sequence[Path] | None = None,
) -> int:
    """Run nl-shell. ``targets_root`` and ``os_release_paths`` are injectable for tests."""
    parser = argparse.ArgumentParser(prog="nl-shell", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"nl-shell {__version__}")
    parser.add_argument(
        "--target",
        metavar="ID",
        help="use this OS target even if it does not match this OS",
    )
    args = parser.parse_args(argv)

    root = targets.TARGETS_ROOT if targets_root is None else targets_root
    paths = (
        os_release.DEFAULT_PATHS if os_release_paths is None else tuple(os_release_paths)
    )
    config_file = config.config_path()
    try:
        settings = config.load(config_file)
        named = args.target if args.target is not None else settings.target
        selection = targets.select(named, root, paths)
    except targets.NoMatchingTarget as exc:
        names = ", ".join(exc.available) or "none"
        print(
            f"nl-shell: {exc}.\n"
            f"Available targets: {names}.\n"
            f"Name one with --target <id>, or with target = \"<id>\" in {config_file}.",
            file=sys.stderr,
        )
        return 1
    except (config.ConfigError, targets.TargetError) as exc:
        print(f"nl-shell: {exc}", file=sys.stderr)
        return 1

    if not selection.matches:
        print(
            f"nl-shell: warning: target {selection.target.id} does not match this OS "
            f"(detected: {selection.detected_id} {selection.detected_version_id}).",
            file=sys.stderr,
        )
    status_lines = tuple(_constant(line) for line in selection.status_lines())
    return Repl.interactive(status_lines=status_lines).run()


def _constant(text: str) -> StatusLine:
    return lambda: text
