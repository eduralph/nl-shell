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

- Every change starts from an issue on the open milestone; the PR body links it
  (`Closes #NNN`, or `Refs #NNN` for partial work — the `require-linked-issue` check
  enforces it).
- One logical change per commit / PR; split unrelated changes.
- A change ships with the means to verify it — a test, or a stated reason why a test
  is impractical plus a manual repro.
- Keep the test suite green: `python3 -m pytest` (from the repo root). First-time setup:
  `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Tests import from `src/`
  directly, so they always run against the tree they sit in.
- **Tests never touch your real environment.** `tests/conftest.py` gives every test a
  temporary `HOME`, `HISTFILE` and XDG directories, and puts guard scripts ahead of the real
  `sudo`, `su`, `pkexec` and `doas` (they exit 99). Tests that need a real environment use a
  marker and run only when it is available:

  | Marker | Runs only when | For |
  |---|---|---|
  | `sudo` | `NL_SHELL_TEST_VM=1` | real sudo-rs, in the throwaway reference VM (M1) |
  | `vm` | `NL_SHELL_TEST_VM=1` | anything else that must run in the reference VM |
  | `gpu` | `NL_SHELL_TEST_GPU=1` | model inference or training on a GPU |
  | `teacher` | `NL_SHELL_TEST_TEACHER=1` | calls to the teacher endpoint |

  Never set `NL_SHELL_TEST_VM=1` on your own machine: `sudo` tests then use the real sudo.
- **Build-time code lives apart from the shipped package.** The data pipeline and training
  code (M1–M3) go under `pipeline/` as the optional extra `nl-shell[pipeline]`, with tests
  under `tests/pipeline/` carrying the markers above. The runtime package `nl_shell` never
  imports it, so the shipped install has no PyTorch and the default test run needs no VM,
  GPU or teacher.

The cycle that drives changes here is run from the companion PDCA instance repo,
[nl-shell-pdca](https://github.com/getwyrd/nl-shell-pdca).
