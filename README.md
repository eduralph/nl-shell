# nl-shell

Natural-language shell for Ubuntu 26.04: a small local model proposes commands, bash runs them, a deterministic policy gate and the human stay in the loop

## What it is

An experiment in replacing bash *as the interface* while bash stays the executor. You type
what you want in plain language; a small model running on your own GPU proposes the command;
a deterministic policy gate decides whether it runs directly or waits for your confirmation;
a persistent bash session runs it. `sudo` works without leaving the REPL, and the password
never reaches the model, the logs or the history.

- **Locally complete** — no cloud endpoints, no API keys, no telemetry; works with the
  network unplugged.
- **Ubuntu 26.04 native** — trained on sudo-rs, uutils, Wayland and systemd-only habits.
- **Target hardware** — any 16 GB NVIDIA or AMD Radeon card (CUDA, ROCm or Vulkan via
  llama.cpp), with a slow but working CPU-only fallback. No training needed to install.
- **Human in the loop** — anything that changes the system is shown and confirmed first.

The full design, phases and open questions are in [docs/plan.md](docs/plan.md).

## Status

Early. See the [open milestone](https://github.com/eduralph/nl-shell/milestones) for
what is in work.

## Development process

Changes to this repo are driven by a PDCA quality cycle (Plan → Do → Check → Act) run from
the companion instance repo
[nl-shell-pdca](https://github.com/eduralph/nl-shell-pdca), rendered from
[pdca-harness](https://github.com/eduralph/pdca-harness). The instance keeps the per-issue
result bundles and the process log; this repo keeps only the product.

Every change starts from an issue on the open milestone and ships as one pull request that
closes it. See [CONTRIBUTING.md](CONTRIBUTING.md) and, for coding agents, [AGENTS.md](AGENTS.md).

## Releases

Versions are git tags of the form `vX.Y.0` — a minor bump per release (patch stays `0`);
each tag is annotated with its release notes (`git show vX.Y.0`). The queue for the next
release is a single open **GitHub milestone**:

- There is always **one open milestone — the next version to ship** (the highest released
  tag's minor `+ 1`). It is the live "in work" list.
- **New issues are filed against that open milestone.** An issue with no milestone carries
  the `new` label (set by the Issue triage workflow) until it is triaged.
- **When that version is released** — cut as a `vX.Y.0` tag — its milestone is **closed and a
  new milestone one minor higher is opened**. New issues then go there. Repeat.

Each change ships as its own pull request with a linked issue (one logical change per PR).

## License

Copyright © 2026 Eduard Ralph.

nl-shell is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE), [NOTICE](NOTICE)).

Contributions are accepted under Apache-2.0 and gated on the
[Developer Certificate of Origin](DCO) — sign off commits with `git commit -s`. See
[CONTRIBUTING.md](CONTRIBUTING.md).
