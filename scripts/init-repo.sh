#!/usr/bin/env bash
# One-time setup for a repo created from pdca-project-template.
#
#   gh repo create <owner>/<name> --public --template eduralph/pdca-project-template \
#     --description "<one line>" --clone
#   cd <name> && scripts/init-repo.sh
#
# GitHub copies a template's files but not its labels, milestones or branch protection,
# so this script:
#   1. fills the {{...}} placeholders from the GitHub repo + git config, drops the
#      template-only README block and itself, then commits (signed off) and pushes to main;
#   2. creates the `new` label the Issue triage workflow uses;
#   3. opens the first milestone ("Milestone 0.1.0");
#   4. turns off the wiki and protects main: `require-linked-issue` required, admins
#      included, conversations resolved, no force-push or deletion.
#
# Steps 2-4 are idempotent; re-running after step 1 only re-applies settings.
set -euo pipefail

FIRST_MILESTONE="${FIRST_MILESTONE:-Milestone 0.1.0}"

die() { echo "init-repo: $*" >&2; exit 1; }

command -v gh >/dev/null || die "gh is not installed"
command -v python3 >/dev/null || die "python3 is not installed"
gh auth status >/dev/null 2>&1 || die "run 'gh auth login' first"
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git checkout"

read -r REPO IS_TEMPLATE BRANCH < <(gh repo view --json nameWithOwner,isTemplate,defaultBranchRef \
  --jq '[.nameWithOwner, .isTemplate, .defaultBranchRef.name] | @tsv')
[ "$IS_TEMPLATE" = "true" ] && die "$REPO is the template itself; run this in a repo created from it"
OWNER="${REPO%%/*}"
NAME="${REPO##*/}"
DESCRIPTION="$(gh repo view --json description --jq .description)"
AUTHOR="$(git config user.name || true)"
[ -n "$AUTHOR" ] || AUTHOR="$(gh api user --jq '.name // .login')"
YEAR="$(date +%Y)"

# --- 1. placeholders --------------------------------------------------------------
if grep -q '{{PROJECT_NAME}}' README.md 2>/dev/null; then
  [ -z "$(git status --porcelain)" ] || die "working tree not clean; commit or stash first"
  [ -n "$DESCRIPTION" ] || die "set a repo description first: gh repo edit --description '...'"
  echo "Filling placeholders for $REPO"
  python3 -I - "$NAME" "$DESCRIPTION" "$OWNER" "$YEAR" "$AUTHOR" <<'PY'
import re, sys
name, desc, owner, year, author = sys.argv[1:]
subs = {"{{PROJECT_NAME}}": name, "{{PROJECT_DESCRIPTION}}": desc,
        "{{OWNER}}": owner, "{{YEAR}}": year, "{{AUTHOR}}": author}
for path in ("README.md", "AGENTS.md", "CONTRIBUTING.md", "NOTICE"):
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"\n<!-- template:begin -->.*?<!-- template:end -->\n\n", "\n", text, flags=re.S)
    for key, value in subs.items():
        text = text.replace(key, value)
    open(path, "w", encoding="utf-8").write(text)
PY
  git rm -q scripts/init-repo.sh
  git add README.md AGENTS.md CONTRIBUTING.md NOTICE
  git commit -q -s -m "chore: initialize from pdca-project-template" \
    -m "Fill the project placeholders and drop the template-only setup script."
  git push -q origin "HEAD:$BRANCH"
else
  echo "Placeholders already filled; re-applying GitHub settings only"
fi

# --- 2. label -----------------------------------------------------------------------
gh label create new --repo "$REPO" --color FBCA04 \
  --description "Not yet assigned to a milestone (set by the Issue triage workflow)" --force >/dev/null
echo "Label 'new' ready"

# --- 3. first milestone -------------------------------------------------------------
if gh api "repos/$REPO/milestones?state=all" --paginate --jq '.[].title' | grep -qxF "$FIRST_MILESTONE"; then
  echo "Milestone '$FIRST_MILESTONE' already exists"
else
  gh api "repos/$REPO/milestones" -f title="$FIRST_MILESTONE" >/dev/null
  echo "Milestone '$FIRST_MILESTONE' created"
fi

# --- 4. repo settings + branch protection ------------------------------------------
gh repo edit "$REPO" --enable-wiki=false >/dev/null
gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" --input - >/dev/null <<'JSON'
{
  "required_status_checks": {"strict": false, "contexts": ["require-linked-issue"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
echo "Branch protection on $BRANCH: require-linked-issue required, admins included"
echo "Done. File issues against '$FIRST_MILESTONE'; every change goes through a PR that closes one."
