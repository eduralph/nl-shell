#!/usr/bin/env python3
"""Docs checks for nl-shell's design records. Standard library only.

Run from the root of any git checkout (a worktree is fine; only `git` is used):

    python scripts/docs_checks.py immutability BASE HEAD

immutability -- a design record that is frozen at the PR's base stays byte-identical at
    its head, except that its frontmatter `status:` line may move to a listed next state.
    Sources: ADR-0001 (docs/design/adr/0001-record-architecture-decisions.md:18-23),
    the class table in docs/design/README.md:18-21, docs/design/proposals/README.md:10-11,
    and the status vocabularies in docs/design/templates/{adr,proposal,spec}.md:4.

    What the PR changed is the diff merge-base -> HEAD. A record counts as frozen if it is
    frozen at the merge-base OR at the base tip (following renames the base branch made
    since the merge-base), so a record frozen on the base after the PR branched off is
    frozen too.

Exit status: 0 = clean, 1 = violations found (listed on stdout), 2 = usage or git error.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass

# --- immutability ---------------------------------------------------------------------

ADR_RE = re.compile(r"^docs/design/adr/\d{4}-[^/]*\.md$")
PROPOSAL_RE = re.compile(r"^docs/design/proposals/(?:draft|accepted)/\d{4}-[^/]*\.md$")
SPEC_RE = re.compile(r"^docs/design/specs/(?:.+/)?[^/]+\.md$")

SUPERSEDED_BY_ADR_RE = re.compile(r"^superseded by adr-\d{4}")


@dataclass(frozen=True)
class RecordClass:
    name: str
    how_to_change: str

    def frozen(self, status: str) -> bool:
        raise NotImplementedError

    def allowed_next(self, base: str, head: str) -> bool:
        raise NotImplementedError


class AdrClass(RecordClass):
    # templates/adr.md:4 -- Proposed | Accepted | Superseded by ADR-NNNN. ADR-0001:22-23:
    # "An ADR starts as Proposed. Once Accepted it MUST NOT be edited". There is no
    # implemented state for an ADR.
    def frozen(self, status: str) -> bool:
        return not status.startswith("proposed")

    def allowed_next(self, base: str, head: str) -> bool:
        return base.startswith("accepted") and bool(SUPERSEDED_BY_ADR_RE.match(head))


class ProposalClass(RecordClass):
    # templates/proposal.md:4 -- draft | accepted | implemented | withdrawn.
    # proposals/README.md:10-11: accepted proposals are never edited; supersession is
    # recorded in the index, not on the frozen file.
    def frozen(self, status: str) -> bool:
        return status != "draft"

    def allowed_next(self, base: str, head: str) -> bool:
        return base == "accepted" and head in ("implemented", "withdrawn")


class SpecClass(RecordClass):
    # templates/spec.md:4 -- draft | stable | superseded | withdrawn.
    # docs/design/README.md:18: "a stable version is never edited, only replaced by a new
    # version".
    def frozen(self, status: str) -> bool:
        return status != "draft"

    def allowed_next(self, base: str, head: str) -> bool:
        return base == "stable" and head in ("superseded", "withdrawn")


ADR = AdrClass("ADR", "write a new ADR that supersedes it and record that in adr/README.md")
PROPOSAL = ProposalClass(
    "proposal", "write a new proposal and record the supersession in proposals/README.md"
)
SPEC = SpecClass(
    "spec", "add a new spec version (e.g. vN+1.md) and record it in specs/README.md"
)


def record_class(path: str) -> RecordClass | None:
    if ADR_RE.match(path):
        return ADR
    if PROPOSAL_RE.match(path):
        return PROPOSAL
    if SPEC_RE.match(path) and posixpath.basename(path) != "README.md":
        return SPEC
    return None


def split_lines(data: bytes) -> list[bytes]:
    """Lines of a blob, each keeping its exact terminator, so CRLF != LF."""
    return data.splitlines(keepends=True)


def terminator(line: bytes) -> bytes:
    return line[len(line.rstrip(b"\r\n")) :]


def line_text(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").rstrip("\r\n")


UTF8_BOM = b"\xef\xbb\xbf"


def status_line_index(lines: list[bytes]) -> int | None:
    """Index of the `status:` line inside the leading YAML frontmatter, if any.

    A leading UTF-8 byte-order mark is ignored here, so a record saved with one is still
    read as having frontmatter (and can be frozen). The byte-exact comparison in
    `compare` still sees the BOM, so adding or removing it is an edit.
    """
    if not lines or line_text(lines[0].removeprefix(UTF8_BOM)) != "---":
        return None
    for i in range(1, len(lines)):
        text = line_text(lines[i])
        if text in ("---", "..."):
            return None
        if re.match(r"^status\s*:", text, re.IGNORECASE):
            return i
    return None


def status_value(line: bytes) -> str:
    value = line_text(line).split(":", 1)[1]
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value.lower()


class GitError(Exception):
    pass


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def git_blob(rev: str, path: str) -> bytes | None:
    """The exact bytes of `path` at `rev` (no newline translation), or None if absent."""
    spec = f"{rev}:{path}"
    probe = subprocess.run(["git", "cat-file", "-e", spec], capture_output=True)
    if probe.returncode != 0:
        return None
    result = subprocess.run(["git", "cat-file", "blob", spec], capture_output=True)
    if result.returncode != 0:
        raise GitError(f"git cat-file blob {spec}: {result.stderr.decode().strip()}")
    return result.stdout


def changed_paths(base: str, head: str) -> list[tuple[str, str, str | None]]:
    """(status letter, old path, new path) for every change from base to head.

    Renames are detected so that a renamed record is checked at its OLD path.
    """
    out = git("diff", "--name-status", "-z", "-M", base, head)
    fields = out.split("\0")
    changes = []
    i = 0
    while i < len(fields) and fields[i]:
        letter = fields[i][0]
        if letter in "RC":
            changes.append((letter, fields[i + 1], fields[i + 2]))
            i += 3
        else:
            changes.append((letter, fields[i + 1], None))
            i += 2
    return changes


def frozen_status(
    cls: RecordClass, blob: bytes | None
) -> tuple[list[bytes], int, str] | None:
    """(lines, status-line index, status) if `blob` is a frozen record, else None."""
    if blob is None:
        return None
    lines = split_lines(blob)
    idx = status_line_index(lines)
    if idx is None:
        return None  # no frontmatter status: not frozen (r2)
    status = status_value(lines[idx])
    if not cls.frozen(status):
        return None
    return lines, idx, status


def compare(
    cls: RecordClass, where: str, frozen: tuple[list[bytes], int, str],
    frozen_path: str, head_path: str | None, head_blob: bytes | None,
) -> str | None:
    """A violation message if the head breaks a record frozen at one base, or None.

    `frozen_path` is where the frozen copy lives at that base; `head_path` is where the
    PR's head keeps the file (None if the PR deleted it).
    """
    base_lines, idx, base_status = frozen
    advice = f"Do not edit it; {cls.how_to_change}."
    where = f"{where} ({cls.name}, status '{base_status}')"
    if head_path is None:
        return f"{where} is frozen and was deleted. {advice}"
    if head_path != frozen_path:
        return f"{where} is frozen and the PR has it at {head_path} instead. {advice}"
    if head_blob is None:
        return f"{where} is frozen and is missing at the head. {advice}"

    head_lines = split_lines(head_blob)
    if head_lines == base_lines:
        return None  # e.g. a mode-only change
    differing = [
        i
        for i in range(max(len(base_lines), len(head_lines)))
        if i >= len(base_lines)
        or i >= len(head_lines)
        or base_lines[i] != head_lines[i]
    ]
    status_only = (
        differing == [idx]
        and status_line_index(head_lines) == idx
        and terminator(base_lines[idx]) == terminator(head_lines[idx])
    )
    if not status_only:
        return f"{where} is frozen and bytes other than its status changed. {advice}"
    head_status = status_value(head_lines[idx])
    if cls.allowed_next(base_status, head_status):
        return None
    return (
        f"{where} is frozen; its status may not change to '{head_status}'. "
        f"{advice}"
    )


@dataclass(frozen=True)
class Base:
    """One commit a record can be frozen at.

    `renames` maps a path at the merge-base to where that file lives at this commit, for
    files the base branch renamed after the PR branched off (empty for the merge-base).
    """

    label: str
    rev: str
    renames: dict[str, str]


def check_record(
    bases: list[Base], rev_head: str, letter: str, path: str, new: str | None
) -> str | None:
    """A violation message for one changed path (merge-base -> head), or None.

    `bases` holds the merge-base and, if different, the base tip. The path is frozen if
    it is frozen at ANY of them -- at the base tip, following any rename the base branch
    made since the merge-base. The head must then keep that frozen copy, at the same path,
    byte-identical except for an allowed status-line change.
    """
    if letter in "AC":  # a new file (or a copy, which leaves the source untouched)
        return None
    head_path = None if letter == "D" else (new if letter == "R" else path)
    head_blob = None if head_path is None else git_blob(rev_head, head_path)
    for base in bases:
        frozen_path = base.renames.get(path, path)
        cls = record_class(frozen_path)
        if cls is None:
            continue
        frozen = frozen_status(cls, git_blob(base.rev, frozen_path))
        if frozen is None:
            continue
        where = f"{path} (frozen at the {base.label}"
        if frozen_path != path:
            where += f", where the base branch moved it to {frozen_path}"
        msg = compare(cls, where + ")", frozen, frozen_path, head_path, head_blob)
        if msg:
            return msg
    return None


def renames_between(old: str, new: str) -> dict[str, str]:
    """{old path: new path} for every file renamed from commit `old` to commit `new`."""
    return {src: dst for letter, src, dst in changed_paths(old, new) if letter == "R"}


def cmd_immutability(base: str, head: str) -> int:
    base_tip = git("rev-parse", "--verify", f"{base}^{{commit}}").strip()
    head_rev = git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
    # Diff from the merge-base (what the PR itself changes, as a PR view shows it), but a
    # record frozen on the base branch after the PR branched off is frozen too -- also when
    # the base branch moved it (e.g. ratified a proposal from draft/ to accepted/).
    merge_base = git("merge-base", base_tip, head_rev).strip()
    bases = [Base("merge-base", merge_base, {})]
    if base_tip != merge_base:
        bases.append(Base("base tip", base_tip, renames_between(merge_base, base_tip)))
    problems = []
    for letter, path, new in changed_paths(merge_base, head_rev):
        msg = check_record(bases, head_rev, letter, path, new)
        if msg:
            problems.append(msg)
    for msg in problems:
        print(f"docs-immutability: {msg}")
    if problems:
        print(
            f"docs-immutability: {len(problems)} frozen design record(s) changed. "
            "Accepted ADRs, non-draft proposals and non-draft specs are append-only: "
            "supersede them with a new record plus an index entry instead of editing."
        )
        return 1
    print("docs-immutability: OK")
    return 0


def main(argv: list[str]) -> int:
    usage = "usage: docs_checks.py immutability BASE HEAD"
    try:
        # Every path below is relative to the repository root, wherever we were started.
        os.chdir(git("rev-parse", "--show-toplevel").strip())
        if len(argv) == 3 and argv[0] == "immutability":
            return cmd_immutability(argv[1], argv[2])
    except GitError as exc:
        print(f"docs_checks: {exc}", file=sys.stderr)
        return 2
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
