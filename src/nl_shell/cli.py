"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from nl_shell import __version__, config, os_release, prompts, render, targets
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
        loaded = prompts.load(selection.target.folder, prompts.user_path())
    except targets.NoMatchingTarget as exc:
        names = ", ".join(exc.available) or "none"
        _error(
            f"nl-shell: {exc}.\n"
            f"Available targets: {names}.\n"
            f"Name one with --target <id>, or with target = \"<id>\" in {config_file}."
        )
        return 1
    except (config.ConfigError, targets.TargetError, prompts.PromptsError) as exc:
        _error(f"nl-shell: {exc}")
        return 1

    if not selection.matches:
        _error(
            f"nl-shell: warning: target {selection.target.id} does not match this OS "
            f"(detected: {selection.detected_id} {selection.detected_version_id})."
        )
    lines = (*selection.status_lines(), *loaded.status_lines())
    status_lines = tuple(_constant(line) for line in lines)
    # No backend until #77 adds the model client: the REPL says so for every request.
    return Repl.interactive(status_lines=status_lines, prompts=loaded).run()


def _error(text: str) -> None:
    """Print a start-up message. File text and paths in it are untrusted: escape them."""
    print(render.escape(text), file=sys.stderr)


def _constant(text: str) -> StatusLine:
    return lambda: text
