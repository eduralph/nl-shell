# Contributing to nl-shell

Thanks for contributing. This project is licensed under the
[Apache License, Version 2.0](LICENSE); contributions are accepted under the same
license.

## Developer Certificate of Origin (sign-off)

Contributions are gated on the [Developer Certificate of Origin](DCO) (DCO 1.1).
By signing off you certify the DCO's terms for your contribution. Sign off every
commit with:

```bash
git commit -s
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer (using your
`git config user.name` / `user.email`). Amend a commit that's missing it with
`git commit --amend -s`.

## Engineering discipline

- Every change starts from an issue on the open milestone; the PR closes it
  (`Closes #NNN` in the PR body — the `require-linked-issue` check enforces it).
- One logical change per commit / PR; split unrelated changes.
- A change ships with the means to verify it — a test, or a stated reason why a test
  is impractical plus a manual repro.
- Keep the test suite green: `python3 -m pytest` (from the repo root). First-time setup:
  `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Tests import from `src/`
  directly, so they always run against the tree they sit in.

The cycle that drives changes here is run from the companion PDCA instance repo,
[nl-shell-pdca](https://github.com/eduralph/nl-shell-pdca).
