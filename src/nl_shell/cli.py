"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from nl_shell import (
    __version__,
    config,
    os_release,
    prompts,
    render,
    targets,
    teacher_client,
)
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
    parser.add_argument(
        "--dev-teacher",
        nargs="?",
        const=True,
        default=None,
        metavar="URL",
        help=(
            "for this session, use the model server at URL (or teacher_url in "
            "config.toml) instead of the local socket; development only"
        ),
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
    # ADR-0003, ADR-0020 §3: the default socket unless --dev-teacher says otherwise.
    endpoint, messages = teacher_client.select_endpoint(
        args.dev_teacher, settings.teacher_url, os.environ.get("XDG_RUNTIME_DIR")
    )
    for message in messages:
        _error(f"nl-shell: {message}")
    client = teacher_client.TeacherClient(
        endpoint, timeout_s=settings.model_timeout_s, model=settings.teacher_model
    )
    lines = (
        *selection.status_lines(),
        *loaded.status_lines(),
        f"model endpoint: {teacher_client.describe_endpoint(endpoint)}",
        f"model backend: {_backend_kind(endpoint)}",
    )
    status_lines = tuple(_constant(line) for line in lines)
    return Repl.interactive(
        status_lines=status_lines, backend=client, prompts=loaded
    ).run()


def _backend_kind(endpoint: teacher_client.Endpoint) -> str:
    if endpoint.kind == "unix":
        return "local model server (chat-completions over the socket)"
    return "dev teacher (chat-completions over TCP, --dev-teacher)"


def _error(text: str) -> None:
    """Print a start-up message. File text and paths in it are untrusted: escape them."""
    print(render.escape(text), file=sys.stderr)


def _constant(text: str) -> StatusLine:
    return lambda: text
