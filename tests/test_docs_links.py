"""The relative-link check (scripts/docs_links.py) for nl-shell's Markdown files.

Each case builds a throwaway git repo in tmp_path, commits a few files and runs the check
as a subprocess. The script is never imported, so a missing script fails each test
instead of breaking collection. Every must-fail case checks that the file and the link
are named in the output, so a missing or crashing script cannot satisfy it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "docs_links.py"

# The tests run with a temporary HOME (tests/conftest.py), so git gets its identity here.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "docs-links test",
    "GIT_AUTHOR_EMAIL": "docs-links@example.invalid",
    "GIT_COMMITTER_NAME": "docs-links test",
    "GIT_COMMITTER_EMAIL": "docs-links@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> None:
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def run_check(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        timeout=60,
    )


# Tracked targets every fixture has: a file, a file in a subdirectory, and so a directory.
BASE_FILES = {
    "README.md": "# Readme\n",
    "docs/guide.md": "# Guide\n\n## Setup\n",
}

DOC = "docs/page.md"

MUST_FAIL = {
    "inline": ("See [the guide](missing.md) here.\n", "missing.md"),
    "image": ("![diagram](img/missing.png)\n", "img/missing.png"),
    "reference definition": ("Text [t].\n\n[t]: missing.md\n", "missing.md"),
    "fragment": ("See [x](missing.md#x).\n", "missing.md#x"),
    "nested-list reference definition": (
        "- item\n  - nested item\n\n    [t]: nested-missing.md\n",
        "nested-missing.md",
    ),
    "after inline code on the same line": (
        "Run `make` then read [this](after-code.md).\n",
        "after-code.md",
    ),
    "balanced parentheses in dest": ("See [one](missing(1).md).\n", "missing(1).md"),
    "with a title": ('See [g](title-missing.md "The guide").\n', "title-missing.md"),
    # A fence must close: a broken link after the closing fence is still checked.
    "after a closed backtick fence": (
        "```\n[a](guide.md)\n```\n[y](after-fence.md)\n",
        "after-fence.md",
    ),
    "after a closed tilde fence": (
        "~~~\n[a](guide.md)\n~~~\n[y](after-tilde.md)\n",
        "after-tilde.md",
    ),
    "after a closed fence on a list-marker line": (
        "- ```\n  [a](guide.md)\n  ```\n\n[y](after-list-fence.md)\n",
        "after-list-fence.md",
    ),
    "after a closed fence in a block quote": (
        "> ```\n> [a](guide.md)\n> ```\n\n[y](after-quote-fence.md)\n",
        "after-quote-fence.md",
    ),
    # A fence closes only on a run at least as long as the opener.
    "after a longer fence closes past a shorter inner run": (
        "````\n```\n[a](guide.md)\n````\n[y](after-long-fence.md)\n",
        "after-long-fence.md",
    ),
    # A backslash-escaped backtick does not open a code span.
    "after an escaped backtick": ("\\`see [x](esc-missing.md) `y`\n", "esc-missing.md"),
    # A code span does not run across a blank line.
    "backtick run does not span paragraphs": (
        "`a\n\n[x](para-missing.md) `\n",
        "para-missing.md",
    ),
    "reference definition after quote marker": ("> [t]: missing.md\n", "missing.md"),
    # A closing run longer than the opener still closes the fence.
    "after a longer closing run": (
        "```\n[a](guide.md)\n````\n[y](after-longer.md)\n",
        "after-longer.md",
    ),
    # The link text is rescanned, so an image inside a link (a badge) is checked too.
    "image inside a link (badge)": ("[![b](missing.png)](guide.md)\n", "missing.png"),
}


@pytest.mark.parametrize("body, dest", MUST_FAIL.values(), ids=MUST_FAIL.keys())
def test_broken_link_fails_and_names_file_and_link(tmp_path, body, dest):
    repo = make_repo(tmp_path, {**BASE_FILES, DOC: body})
    result = run_check(repo)
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert DOC in result.stdout, out
    assert dest in result.stdout, out
    assert f"{DOC}:" in result.stdout and f"broken link: {dest}" in result.stdout, out


MUST_PASS = {
    "external https and mailto": (
        "[site](https://example.com/missing.md) and [mail](mailto:someone@example.com)\n"
    ),
    "pure fragment": "Jump to [setup](#setup).\n",
    "valid file": "See [guide](guide.md) and [readme](../README.md).\n",
    "valid directory": "See [docs](../docs) and [root](../) and [here](./).\n",
    "valid file with fragment": "See [setup](guide.md#setup).\n",
    "comment spanning lines": "Before.\n<!--\nSee [x](missing.md).\n-->\nAfter.\n",
    "unclosed comment masks the rest": "Before.\n<!-- open\n\n[x](missing.md)\n\nmore\n",
    "backtick fence": "```markdown\n[x](missing.md)\n```\n",
    "tilde fence": "~~~\n[x](missing.md)\n~~~\n",
    "fence on a list-marker line": "- ```\n  [x](missing.md)\n  ```\n",
    "fence in a block quote": "> ```\n> [x](missing.md)\n> ```\n",
    "fence inside a comment": "<!--\n```\n-->\n[ok](guide.md)\n",
    "inline code single backticks": "Write `[x](missing.md)` literally.\n",
    "inline code double backticks": "Write ``[x](missing.md) with ` inside`` literally.\n",
    "footnote definition": "Text[^1].\n\n[^1]: missing.md\n",
    "escaped bracket": "Not a link: \\[x](missing.md).\n",
    "no closing paren (prose)": "See [x](missing.md for more.\n",
    "valid file with a title": 'See [g](guide.md "The guide") and [s](guide.md (Setup)).\n',
    "shorter inner run does not close a fence": "````\n```\n[x](missing.md)\n````\n",
    "other fence character does not close a fence": "```\n~~~\n[x](missing.md)\n```\n",
    # Closed by a longer run, so inline code cannot pair the two runs: only the fence
    # rule (with `>` markers stripped) hides this link.
    "fence in a block quote closed by a longer run": "> ```\n> [x](missing.md)\n> ````\n",
}


@pytest.mark.parametrize("body", MUST_PASS.values(), ids=MUST_PASS.keys())
def test_link_that_is_not_checked_or_resolves_passes(tmp_path, body):
    repo = make_repo(tmp_path, {**BASE_FILES, DOC: body})
    result = run_check(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout, result.stdout + result.stderr


def test_backslash_escapes_in_dest_are_undone(tmp_path):
    # `gu\_ide.md` names the tracked file `gu_ide.md` once the escape is undone.
    files = {**BASE_FILES, "docs/gu_ide.md": "# G\n", DOC: "See [g](gu\\_ide.md).\n"}
    repo = make_repo(tmp_path, files)
    result = run_check(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 relative links" in result.stdout, result.stdout
    assert "0 broken" in result.stdout, result.stdout


def test_fence_inside_comment_does_not_hide_a_later_broken_link(tmp_path):
    # Step 1 removes the comment before step 2 looks for fences, so no stray fence opens
    # and the broken link after the comment is still checked.
    body = "<!--\n```\n-->\n[x](after-comment.md)\n"
    repo = make_repo(tmp_path, {**BASE_FILES, DOC: body})
    result = run_check(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{DOC}:4: broken link: after-comment.md" in result.stdout, result.stdout


def test_live_checkout_has_no_broken_links():
    # Runs on this nl-shell checkout. `git` resolves the repo itself, so it also works
    # when `.git` is a file (a worktree).
    result = run_check(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout, result.stdout + result.stderr
