"""The docs check (scripts/docs_checks.py) that enforces ADR-0001's immutability rule.

ADR-0001 (docs/design/adr/0001-record-architecture-decisions.md:22-23, :30-31) says an
Accepted ADR is never edited and that a docs CI check enforces it;
docs/design/README.md:18-21 and docs/design/proposals/README.md:10-11 say the same for
stable specs and accepted proposals. Each test builds a throwaway git repo, makes a base
and a head commit, and runs the check as a subprocess. The script is never imported, so a
missing script fails each test instead of breaking collection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "docs_checks.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "docs-check test",
    "GIT_AUTHOR_EMAIL": "docs-check@example.invalid",
    "GIT_COMMITTER_NAME": "docs-check test",
    "GIT_COMMITTER_EMAIL": "docs-check@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}

ADR = "docs/design/adr"
PROP = "docs/design/proposals"
SPEC = "docs/design/specs"

# The status comments the templates ship (docs/design/templates/{adr,proposal,spec}.md:4).
ADR_COMMENT = "   # Proposed | Accepted | Superseded by ADR-NNNN"
PROPOSAL_COMMENT = "   # draft | accepted | implemented | withdrawn"
SPEC_COMMENT = "   # draft | stable | superseded | withdrawn"


def adr(status: str, body: str = "We decide X.") -> str:
    return (
        f"---\ncreated: 14.10.2026 19:22\ntype: adr\nstatus: {status}\n"
        "tags:\n  - adr\n---\n"
        f"# 0042. A decision\n\n## Context\n\nSome context.\n\n## Decision\n\n{body}\n"
    )


def proposal(status: str, body: str = "The plan.") -> str:
    return (
        f"---\ncreated: 14.10.2026 19:22\ntype: proposal\nstatus: {status}\nauthor:\n---\n"
        f"# 0001. A proposal\n\n{body}\n"
    )


def spec(status: str, body: str = "The format MUST be JSON.") -> str:
    return (
        f"---\ncreated: 14.10.2026 19:22\ntype: spec\nstatus: {status}\nversion: v1\n---\n"
        f"# Policy gate v1\n\n{body}\n"
    )


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        env = {**os.environ, **GIT_ENV}
        result = subprocess.run(
            [
                "git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
                "-c", "core.autocrlf=false", *args,
            ],
            cwd=self.root, env=env, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def write(self, files: dict[str, str | bytes | None]) -> None:
        for rel, text in files.items():
            path = self.root / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8")

    def commit(self, files: dict[str, str | bytes | None] | None = None) -> str:
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def move(self, old: str, new: str) -> None:
        (self.root / new).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", old, new)


def run_check(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
    )


def immutability(repo: Repo, base: str, head: str) -> subprocess.CompletedProcess:
    return run_check(repo.root, "immutability", base, head)


def scenario(tmp_path, base_files, head_files, moves=()):
    repo = Repo(tmp_path / "repo")
    base = repo.commit(base_files)
    for old, new in moves:
        repo.move(old, new)
    head = repo.commit(head_files)
    return immutability(repo, base, head)


ADR_PATH = f"{ADR}/0042-a-decision.md"
PROP_ACCEPTED = f"{PROP}/accepted/0001-implementation-arc.md"
PROP_DRAFT = f"{PROP}/draft/0001-implementation-arc.md"
SPEC_PATH = f"{SPEC}/policy-gate/v1.md"


# --- must fail ----------------------------------------------------------------------------

MUST_FAIL = {
    "accepted-adr-body": (
        {ADR_PATH: adr("Accepted")}, {ADR_PATH: adr("Accepted", "We decide Y.")},
    ),
    "superseded-adr-body": (
        {ADR_PATH: adr("Superseded by ADR-0013")},
        {ADR_PATH: adr("Superseded by ADR-0013", "We decide Y.")},
    ),
    "superseded-adr-status": (
        {ADR_PATH: adr("Superseded by ADR-0013")},
        {ADR_PATH: adr("Superseded by ADR-0021")},
    ),
    "superseded-adr-back-to-proposed": (
        {ADR_PATH: adr("Superseded by ADR-0013")}, {ADR_PATH: adr("Proposed")},
    ),
    "accepted-adr-to-implemented": (
        {ADR_PATH: adr("Accepted")}, {ADR_PATH: adr("Implemented")},
    ),
    "accepted-adr-to-proposed": (
        {ADR_PATH: adr("Accepted")}, {ADR_PATH: adr("Proposed")},
    ),
    "accepted-adr-status-and-body": (
        {ADR_PATH: adr("Accepted")},
        {ADR_PATH: adr("Superseded by ADR-0050", "We decide Y.")},
    ),
    "accepted-adr-deleted": ({ADR_PATH: adr("Accepted")}, {ADR_PATH: None}),
    "accepted-proposal-body": (
        {PROP_ACCEPTED: proposal("accepted")},
        {PROP_ACCEPTED: proposal("accepted", "Another plan.")},
    ),
    "accepted-proposal-to-superseded": (
        {PROP_ACCEPTED: proposal("accepted")}, {PROP_ACCEPTED: proposal("superseded")},
    ),
    "implemented-proposal-body": (
        {PROP_ACCEPTED: proposal("implemented")},
        {PROP_ACCEPTED: proposal("implemented", "Another plan.")},
    ),
    "implemented-proposal-status": (
        {PROP_ACCEPTED: proposal("implemented")}, {PROP_ACCEPTED: proposal("withdrawn")},
    ),
    "withdrawn-proposal-body": (
        {PROP_ACCEPTED: proposal("withdrawn")},
        {PROP_ACCEPTED: proposal("withdrawn", "Another plan.")},
    ),
    "stable-spec-body": (
        {SPEC_PATH: spec("stable")},
        {SPEC_PATH: spec("stable", "The format MUST be YAML.")},
    ),
    "stable-spec-to-draft": ({SPEC_PATH: spec("stable")}, {SPEC_PATH: spec("draft")}),
    "superseded-spec-body": (
        {SPEC_PATH: spec("superseded")},
        {SPEC_PATH: spec("superseded", "The format MUST be YAML.")},
    ),
    "withdrawn-spec-body": (
        {SPEC_PATH: spec("withdrawn")},
        {SPEC_PATH: spec("withdrawn", "The format MUST be YAML.")},
    ),
    "withdrawn-spec-status": (
        {SPEC_PATH: spec("withdrawn")}, {SPEC_PATH: spec("stable")},
    ),
    "superseded-spec-status": (
        {SPEC_PATH: spec("superseded")}, {SPEC_PATH: spec("withdrawn")},
    ),
    "stable-spec-with-template-comment-body": (
        {SPEC_PATH: spec(f"stable{SPEC_COMMENT}")},
        {SPEC_PATH: spec(f"stable{SPEC_COMMENT}", "The format MUST be YAML.")},
    ),
    "quoted-accepted-proposal-body": (
        {PROP_ACCEPTED: proposal('"accepted"')},
        {PROP_ACCEPTED: proposal('"accepted"', "Another plan.")},
    ),
}


@pytest.mark.parametrize("base_files,head_files", MUST_FAIL.values(), ids=MUST_FAIL.keys())
def test_frozen_record_change_fails(tmp_path, base_files, head_files):
    result = scenario(tmp_path, base_files, head_files)
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    (path,) = base_files
    assert path in result.stdout, out
    assert "supersede" in result.stdout, out


def test_renaming_an_accepted_adr_fails_at_its_old_path(tmp_path):
    new = f"{ADR}/0042-renamed.md"
    result = scenario(
        tmp_path, {ADR_PATH: adr("Accepted")}, None, moves=[(ADR_PATH, new)]
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout


def test_moving_an_accepted_proposal_fails(tmp_path):
    other = f"{PROP}/draft/0001-implementation-arc.md"
    result = scenario(
        tmp_path, {PROP_ACCEPTED: proposal("accepted")}, None,
        moves=[(PROP_ACCEPTED, other)],
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert PROP_ACCEPTED in result.stdout


def test_every_offending_path_is_reported(tmp_path):
    other = f"{ADR}/0043-another.md"
    result = scenario(
        tmp_path,
        {ADR_PATH: adr("Accepted"), other: adr("Accepted")},
        {ADR_PATH: adr("Accepted", "Y."), other: adr("Accepted", "Z.")},
    )
    assert result.returncode == 1
    assert ADR_PATH in result.stdout and other in result.stdout


# --- must pass ----------------------------------------------------------------------------

MUST_PASS = {
    "proposed-adr-body": (
        {ADR_PATH: adr("Proposed")}, {ADR_PATH: adr("Proposed", "We decide Y.")},
    ),
    "partly-superseded-proposed-adr-body": (
        {ADR_PATH: adr("Proposed (backend order and VRAM budget superseded by ADR-0019)")},
        {ADR_PATH: adr(
            "Proposed (backend order and VRAM budget superseded by ADR-0019)", "Y."
        )},
    ),
    "adr-proposed-to-accepted": (
        {ADR_PATH: adr("Proposed")}, {ADR_PATH: adr("Accepted")},
    ),
    "adr-proposed-to-accepted-with-edit": (
        {ADR_PATH: adr("Proposed")}, {ADR_PATH: adr("Accepted", "We decide Y.")},
    ),
    "accepted-adr-to-superseded": (
        {ADR_PATH: adr("Accepted")}, {ADR_PATH: adr("Superseded by ADR-0050")},
    ),
    "accepted-adr-to-superseded-with-suffix": (
        {ADR_PATH: adr("Accepted")},
        {ADR_PATH: adr("Superseded by ADR-0050 and ADR-0051 (2026-11-01)")},
    ),
    "accepted-adr-to-superseded-lowercase": (
        {ADR_PATH: adr("accepted")}, {ADR_PATH: adr("superseded by adr-0050")},
    ),
    "accepted-proposal-to-implemented": (
        {PROP_ACCEPTED: proposal("accepted")}, {PROP_ACCEPTED: proposal("implemented")},
    ),
    "accepted-proposal-to-withdrawn": (
        {PROP_ACCEPTED: proposal("accepted")}, {PROP_ACCEPTED: proposal("withdrawn")},
    ),
    "draft-proposal-body": (
        {PROP_DRAFT: proposal("draft")}, {PROP_DRAFT: proposal("draft", "Another plan.")},
    ),
    "draft-spec-body": (
        {SPEC_PATH: spec("draft")}, {SPEC_PATH: spec("draft", "The format MUST be YAML.")},
    ),
    "spec-draft-to-stable": ({SPEC_PATH: spec("draft")}, {SPEC_PATH: spec("stable")}),
    "stable-spec-to-superseded": (
        {SPEC_PATH: spec("stable")}, {SPEC_PATH: spec("superseded")},
    ),
    "stable-spec-to-withdrawn": (
        {SPEC_PATH: spec("stable")}, {SPEC_PATH: spec("withdrawn")},
    ),
    "new-spec-version": (
        {SPEC_PATH: spec("stable")},
        {f"{SPEC}/policy-gate/v2.md": spec("draft")},
    ),
    "new-adr-and-proposal": (
        {ADR_PATH: adr("Accepted")},
        {
            f"{ADR}/0043-new.md": adr("Proposed"),
            f"{PROP}/draft/0002-new.md": proposal("draft"),
        },
    ),
    "indexes-edited": (
        {
            f"{ADR}/README.md": "# ADRs\n",
            f"{PROP}/README.md": "# Proposals\n",
            f"{SPEC}/README.md": "# Specs\n",
            ADR_PATH: adr("Accepted"),
        },
        {
            f"{ADR}/README.md": "# ADRs\n\nEdited.\n",
            f"{PROP}/README.md": "# Proposals\n\nEdited.\n",
            f"{SPEC}/README.md": "# Specs\n\nEdited.\n",
        },
    ),
    "no-frontmatter-status": (
        {ADR_PATH: "# 0042. No frontmatter\n\nBody.\n"},
        {ADR_PATH: "# 0042. No frontmatter\n\nEdited.\n"},
    ),
    "other-docs-edited": (
        {"docs/design/architecture/01-intro.md": "---\nstatus: living\n---\n# Intro\n"},
        {"docs/design/architecture/01-intro.md": "---\nstatus: living\n---\n# Intro 2\n"},
    ),
    # A new record copied from the template still carries the status comment
    # (docs/design/templates/{spec,proposal}.md:4); it is a draft and stays editable.
    "draft-spec-with-template-comment-body": (
        {SPEC_PATH: spec(f"draft{SPEC_COMMENT}")},
        {SPEC_PATH: spec(f"draft{SPEC_COMMENT}", "The format MUST be YAML.")},
    ),
    "draft-proposal-with-template-comment-body": (
        {PROP_DRAFT: proposal(f"draft{PROPOSAL_COMMENT}")},
        {PROP_DRAFT: proposal(f"draft{PROPOSAL_COMMENT}", "Another plan.")},
    ),
    "quoted-draft-spec-body": (
        {SPEC_PATH: spec('"draft"')},
        {SPEC_PATH: spec('"draft"', "The format MUST be YAML.")},
    ),
    "single-quoted-draft-proposal-body": (
        {PROP_DRAFT: proposal("'draft'")},
        {PROP_DRAFT: proposal("'draft'", "Another plan.")},
    ),
    "proposed-adr-with-template-comment-body": (
        {ADR_PATH: adr(f"Proposed{ADR_COMMENT}")},
        {ADR_PATH: adr(f"Proposed{ADR_COMMENT}", "We decide Y.")},
    ),
    "accepted-proposal-with-template-comment-to-implemented": (
        {PROP_ACCEPTED: proposal(f"accepted{PROPOSAL_COMMENT}")},
        {PROP_ACCEPTED: proposal(f"implemented{PROPOSAL_COMMENT}")},
    ),
    "quoted-stable-spec-to-superseded": (
        {SPEC_PATH: spec('"stable"')}, {SPEC_PATH: spec('"superseded"')},
    ),
}


@pytest.mark.parametrize("base_files,head_files", MUST_PASS.values(), ids=MUST_PASS.keys())
def test_allowed_change_passes(tmp_path, base_files, head_files):
    result = scenario(tmp_path, base_files, head_files)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_ratifying_proposal_moves_from_draft_to_accepted(tmp_path):
    """The #6 ratification step: draft/ -> accepted/ while the status becomes accepted."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({PROP_DRAFT: proposal("draft")})
    repo.move(PROP_DRAFT, PROP_ACCEPTED)
    head = repo.commit({PROP_ACCEPTED: proposal("accepted")})
    result = immutability(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_frozen_state_comes_from_the_base_not_the_head(tmp_path):
    """Accepting an ADR in a PR does not freeze it for that PR; accepting it on main
    does."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({ADR_PATH: adr("Proposed")})
    accepted = repo.commit({ADR_PATH: adr("Accepted")})
    edited = repo.commit({ADR_PATH: adr("Accepted", "We decide Y.")})
    assert immutability(repo, base, edited).returncode == 0
    result = immutability(repo, accepted, edited)
    assert result.returncode == 1
    assert ADR_PATH in result.stdout


def diverged(tmp_path, main_files, pr_files, main_moves=(), pr_moves=()):
    """Branch a PR off a Proposed ADR and a draft proposal, then commit `main_files` on
    main and `pr_files` on the PR branch (after any `git mv`s). Returns
    (repo, main tip, PR head)."""
    repo = Repo(tmp_path / "repo")
    repo.commit({
        ADR_PATH: adr("Proposed"),
        PROP_DRAFT: proposal("draft"),
        "docs/other.md": "# Other\n",
    })
    repo.git("branch", "pr")
    for old, new in main_moves:
        repo.move(old, new)
    main_tip = repo.commit(main_files)
    repo.git("checkout", "-q", "pr")
    for old, new in pr_moves:
        repo.move(old, new)
    pr_head = repo.commit(pr_files)
    return repo, main_tip, pr_head


def test_pr_editing_a_draft_proposal_that_main_ratified_and_moved_fails(tmp_path):
    """Main ratifies proposal 0001 (git mv draft/ -> accepted/, status accepted) after the
    PR branched; the PR edits the body at the old draft/ path. Frozen at the base tip under
    its new path, so the edit fails."""
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {PROP_ACCEPTED: proposal("accepted")},
        {PROP_DRAFT: proposal("draft", "Another plan.")},
        main_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert PROP_DRAFT in result.stdout
    assert PROP_ACCEPTED in result.stdout


def test_pr_making_the_same_ratification_as_main_passes(tmp_path):
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {PROP_ACCEPTED: proposal("accepted")},
        {PROP_ACCEPTED: proposal("accepted")},
        main_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
        pr_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pr_ratifying_with_a_different_body_than_main_fails(tmp_path):
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {PROP_ACCEPTED: proposal("accepted")},
        {PROP_ACCEPTED: proposal("accepted", "Another plan.")},
        main_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
        pr_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert PROP_DRAFT in result.stdout


def test_pr_not_touching_a_proposal_main_moved_passes(tmp_path):
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {PROP_ACCEPTED: proposal("accepted")},
        {"docs/other.md": "# Other, edited on the PR\n"},
        main_moves=[(PROP_DRAFT, PROP_ACCEPTED)],
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_adr_accepted_on_main_after_the_pr_branched_is_frozen(tmp_path):
    """Merge-base still says Proposed, but the base tip has it Accepted: the edit fails."""
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {ADR_PATH: adr("Accepted")},
        {ADR_PATH: adr("Proposed", "We decide Y.")},
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout
    assert "base tip" in result.stdout


def test_diverged_pr_editing_a_still_proposed_adr_passes(tmp_path):
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {"docs/other.md": "# Other, edited on main\n"},
        {ADR_PATH: adr("Proposed", "We decide Y.")},
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pr_branched_before_main_superseded_an_adr_passes(tmp_path):
    """Main moves ADR X Accepted -> Superseded by ... after the PR branched; the PR does not
    touch X. Diffing base tip -> head would show the PR "reverting" X; merge-base -> head
    does not."""
    repo = Repo(tmp_path / "repo")
    repo.commit({ADR_PATH: adr("Accepted"), "docs/other.md": "# Other\n"})
    repo.git("branch", "pr")
    main_tip = repo.commit({ADR_PATH: adr("Superseded by ADR-0050")})
    repo.git("checkout", "-q", "pr")
    pr_head = repo.commit({"docs/other.md": "# Other, edited on the PR\n"})
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_diverged_pr_not_touching_the_accepted_adr_passes(tmp_path):
    repo, main_tip, pr_head = diverged(
        tmp_path,
        {ADR_PATH: adr("Accepted")},
        {"docs/other.md": "# Other, edited on the PR\n"},
    )
    result = immutability(repo, main_tip, pr_head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_crlf_rewrite_of_an_accepted_adr_fails(tmp_path):
    """Comparison is byte-exact: LF -> CRLF is an edit, even though the text is the same."""
    lf = adr("Accepted").encode()
    result = scenario(tmp_path, {ADR_PATH: lf}, {ADR_PATH: lf.replace(b"\n", b"\r\n")})
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout


def test_crlf_on_an_allowed_status_line_change_fails(tmp_path):
    base = adr("Accepted").encode()
    head = adr("Superseded by ADR-0050").encode().replace(
        b"status: Superseded by ADR-0050\n", b"status: Superseded by ADR-0050\r\n"
    )
    result = scenario(tmp_path, {ADR_PATH: base}, {ADR_PATH: head})
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout


def test_crlf_accepted_adr_status_only_change_passes(tmp_path):
    """A record stored with CRLF may still take its allowed status-line change."""
    base = adr("Accepted").encode().replace(b"\n", b"\r\n")
    head = adr("Superseded by ADR-0050").encode().replace(b"\n", b"\r\n")
    result = scenario(tmp_path, {ADR_PATH: base}, {ADR_PATH: head})
    assert result.returncode == 0, result.stdout + result.stderr


BOM = b"\xef\xbb\xbf"


def test_accepted_adr_saved_with_a_bom_is_still_frozen(tmp_path):
    """A leading UTF-8 BOM must not hide the frontmatter: the body edit fails."""
    base = BOM + adr("Accepted").encode()
    head = BOM + adr("Accepted", "We decide Y.").encode()
    result = scenario(tmp_path, {ADR_PATH: base}, {ADR_PATH: head})
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout


def test_bom_accepted_adr_status_only_change_passes(tmp_path):
    base = BOM + adr("Accepted").encode()
    head = BOM + adr("Superseded by ADR-0050").encode()
    result = scenario(tmp_path, {ADR_PATH: base}, {ADR_PATH: head})
    assert result.returncode == 0, result.stdout + result.stderr


def test_removing_the_bom_from_an_accepted_adr_fails(tmp_path):
    """Byte-exact: dropping the BOM is an edit, like LF -> CRLF."""
    text = adr("Accepted").encode()
    result = scenario(tmp_path, {ADR_PATH: BOM + text}, {ADR_PATH: text})
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout


# --- the CI step (.github/workflows/docs-checks.yml) ---------------------------------------

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-checks.yml"
STEP_NAME = "Frozen design records are unchanged"


def workflow_step_script() -> str:
    """The `run: |` shell script of the freeze step, read from the workflow file."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP_NAME}")
    run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    body = []
    indent = None
    for line in lines[run + 1 :]:
        if line.strip() and indent is None:
            indent = len(line) - len(line.lstrip())
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:] if line.strip() else "")
    return "\n".join(body) + "\n"


def run_workflow_step(tmp_path: Path, repo: Repo, base: str, head: str):
    """Run the step as GitHub would: in the checkout of the PR head, with BASE/HEAD set
    and `python` on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").symlink_to(sys.executable)
    repo.git("checkout", "-q", head)
    env = {
        **os.environ, **GIT_ENV,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "BASE": base, "HEAD": head,
    }
    return subprocess.run(
        ["bash", "-c", workflow_step_script()],
        cwd=repo.root, env=env, capture_output=True, text=True,
    )


def test_ci_step_runs_the_base_copy_so_a_pr_cannot_disable_the_check(tmp_path):
    """The PR replaces scripts/docs_checks.py with a no-op and edits an Accepted ADR. The
    step runs the base commit's script, so the edit still fails."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({
        "scripts/docs_checks.py": SCRIPT.read_bytes(),
        ADR_PATH: adr("Accepted"),
    })
    head = repo.commit({
        "scripts/docs_checks.py": "import sys\nprint('docs-immutability: OK')\nsys.exit(0)\n",
        ADR_PATH: adr("Accepted", "We decide Y."),
    })
    result = run_workflow_step(tmp_path, repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout
    assert "from the base commit" in result.stdout


SHADOW_RE = "import sys\nprint('docs-immutability: OK')\nsys.exit(0)\n"


def test_ci_step_ignores_a_stdlib_module_planted_by_the_pr(tmp_path):
    """The PR edits an Accepted ADR and adds a top-level `re.py` that prints OK and exits
    0. The base copy of the script imports `re`; it must get the standard library one, so
    the edit still fails."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({
        "scripts/docs_checks.py": SCRIPT.read_bytes(),
        ADR_PATH: adr("Accepted"),
    })
    head = repo.commit({
        "re.py": SHADOW_RE,
        ADR_PATH: adr("Accepted", "We decide Y."),
    })
    result = run_workflow_step(tmp_path, repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout
    assert "from the base commit" in result.stdout


def test_ci_step_fallback_ignores_a_module_planted_next_to_the_script(tmp_path):
    """Same attack on the PR that first adds the script: `scripts/re.py` must not replace
    the standard library `re`."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({ADR_PATH: adr("Accepted")})
    head = repo.commit({
        "scripts/docs_checks.py": SCRIPT.read_bytes(),
        "scripts/re.py": SHADOW_RE,
        ADR_PATH: adr("Accepted", "We decide Y."),
    })
    result = run_workflow_step(tmp_path, repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout
    assert "running the PR's copy" in result.stdout


def test_ci_step_falls_back_to_the_pr_copy_when_the_base_has_no_script(tmp_path):
    """The PR that first adds the script runs its own copy (and still catches an edit)."""
    repo = Repo(tmp_path / "repo")
    base = repo.commit({ADR_PATH: adr("Accepted")})
    head = repo.commit({
        "scripts/docs_checks.py": SCRIPT.read_bytes(),
        ADR_PATH: adr("Accepted", "We decide Y."),
    })
    result = run_workflow_step(tmp_path, repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ADR_PATH in result.stdout
    assert "running the PR's copy" in result.stdout


def test_ci_step_passes_a_clean_pr(tmp_path):
    repo = Repo(tmp_path / "repo")
    base = repo.commit({
        "scripts/docs_checks.py": SCRIPT.read_bytes(),
        ADR_PATH: adr("Accepted"),
    })
    head = repo.commit({ADR_PATH: adr("Superseded by ADR-0050")})
    result = run_workflow_step(tmp_path, repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs-immutability: OK" in result.stdout
