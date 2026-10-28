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

Pre-code. See the [milestones](https://github.com/getwyrd/nl-shell/milestones) for the plan
and what is in work.

## Development process

Changes to this repo are driven by a PDCA quality cycle (Plan → Do → Check → Act) run from
the companion instance repo
[nl-shell-pdca](https://github.com/getwyrd/nl-shell-pdca), rendered from
[pdca-harness](https://github.com/eduralph/pdca-harness). The instance keeps the per-issue
result bundles and the process log; this repo keeps only the product.

Every change starts from an issue on the open milestone and ships as one pull request that
closes it. See [CONTRIBUTING.md](CONTRIBUTING.md) and, for coding agents, [AGENTS.md](AGENTS.md).

## Releases

Work is planned in **named milestones** — `Foundations`, then `M0` to `M5` — that follow
[proposal 0001, the implementation arc](docs/design/proposals/draft/0001-implementation-arc.md).
Each milestone has a tracker issue listing its work items, and its description says what it
proves and when it is done.

- **New issues are filed against the milestone they belong to.** An issue with no milestone
  carries the `new` label (set by the Issue triage workflow) until it is triaged; backlog
  items stay there until a proposal places them.
- **Releases are git tags `vX.Y.0`** cut at a ★ release point, each annotated with its release
  notes. The first is `v0.1.0` at the end of M5.

Each change ships as its own pull request with a linked issue (one logical change per PR).

## Design documentation

[`docs/design/`](docs/design/README.md) holds the architecture overview, the decision records
(ADRs), proposals and specs. Start with its reading order.

## License

Copyright © 2026 Eduard Ralph.

nl-shell is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE), [NOTICE](NOTICE)).

Contributions are accepted under Apache-2.0 and gated on the
[Developer Certificate of Origin](DCO) — sign off commits with `git commit -s`. See
[CONTRIBUTING.md](CONTRIBUTING.md).
