---
created: 14.10.2026 19:22
type: proposal
status: draft
author: Eduard Ralph
tracking-issue: "#5"
tags:
  - proposal
  - roadmap
  - build-order
---
# Proposal: The implementation arc — the order nl-shell is built in

> The milestone plan, after wyrd's implementation arc. It orders the work in
> [`docs/plan.md`](../../../plan.md) into milestones and says, for each, what it proves,
> which risk it retires, what it needs, and when it is done. It records *order*, not dates.
> The GitHub milestones carry the same names and point here. The *why* of each design choice
> is in the ADRs; architecture §9 is the short summary.

## Motivation

nl-shell can be built model-first (collect data, train, then wrap a REPL around it) or
safety-first. Model-first defers the parts that make the project safe to use — the gate, the
confirm, sudo passthrough, taint — until after the expensive training work, and risks
discovering late that the safety layer needs a different model interface.

This proposal commits to **safety first, then the model**. The ordering principle is **risk
retired, not features delivered**, and every milestone is a coherent stopping point: if the
project pauses after any of them, what exists is usable or publishable.

## Design

### Foundations

- **Proves:** the design is written down and the decisions are ratified before code depends
  on them.
- **Retires:** reopening settled questions in every issue; building against an unratified
  decision.
- **Needs:** nothing.
- **Done when:** design-doc structure, architecture overview and this proposal are merged;
  ADRs 0002–0012 are ratified (#6); this proposal is accepted; the threat model exists (#1);
  docs CI runs (#7).
- **Stopping point:** no — groundwork.

### M0 — REPL prototype

- **Proves:** the safety layer works: AST gate, confirm UI, sudo passthrough, taint labels,
  session logging, input modes — on a real PTY, with the teacher proposing.
- **Retires:** the project's largest risk: that a model-driven shell cannot be made safe
  enough to use. Everything that decides what runs is deterministic and testable here,
  independent of which model proposes.
- **Needs:** Foundations ratified. The open questions on interactive programs and env
  filtering are settled inside M0.
- **Done when:** a proposed command runs only through the gate; mutating and privileged
  commands always confirm; tests prove the sudo password reaches no log, history, state block
  or `:context`; sessions are logged as JSONL. Run first in a throwaway VM.
- **Stopping point:** ★ yes. A useful developer tool on the teacher, and the source of real
  session logs for M2.
- **Tracker:** #8.

### M1 — 26.04 reference system

- **Proves:** there is a reproducible Ubuntu 26.04 source of truth and sandbox.
- **Retires:** training on guesses about 26.04; running untrusted candidate commands on the
  developer's machine.
- **Needs:** nothing from M0, so it can run in parallel. M0.5 (sudo passthrough) uses its
  sudo-rs findings.
- **Done when:** the VM rebuilds from a script; the harvest is reproducible from a snapshot;
  sudo-rs prompt and env behaviour is recorded.
- **Stopping point:** soft.
- **Tracker:** #21.

### M2 — Data pipeline

- **Proves:** grounded, execution-verified training data for 26.04 can be produced at scale.
- **Retires:** the risk that the student learns wrong or outdated commands; eval leakage.
- **Needs:** M1 (harvest and sandbox); M0 session logs (optional input); the teacher-license
  and training-data-license checks (release-gating, before generation starts).
- **Done when:** the dataset builds reproducibly; every kept pair passed the execution
  filter; the eval split including 26.04 probes is frozen with a hash before any training.
- **Stopping point:** soft. The dataset alone is publishable if licenses allow.
- **Tracker:** #25.

### M3 — Student model and intent classifier

- **Proves:** a 1.5–3B model can be trained on the 16 GB card for this job, and plain input
  can be routed locally in under 10 ms.
- **Retires:** the latency risk of a large model; the guesswork of the heuristic router.
- **Needs:** M2; the student base-model license check (release-gating).
- **Done when:** the student trains reproducibly with the REPL's prompt format; the
  classifier beats the heuristic on held-out data in under 10 ms on CPU.
- **Stopping point:** soft.
- **Tracker:** #35.

### M4 — Swap and evaluate

- **Proves:** the student is good enough to replace the teacher, fully offline.
- **Retires:** the quality risk of a small model.
- **Needs:** M3; the eval suite.
- **Done when:** the eval targets in architecture §10 are met against the teacher baseline,
  with unrequested destructive proposals at ≈ 0 (blocking).
- **Stopping point:** soft. Usable by the developer offline; not yet installable by others.
- **Tracker:** #41.

### M5 — Packaged release

- **Proves:** anyone with a 16 GB NVIDIA or AMD card on Ubuntu 26.04 can install and use it
  without training.
- **Retires:** vendor and packaging risk; license risk of what ships.
- **Needs:** M4.
- **Done when:** the installer picks CUDA / ROCm / Vulkan / CPU and verifies the weights;
  eval and latency pass on every test-matrix card and CPU-only; the license check of
  everything shipped is done; user docs cover install, input modes, the safety model and
  what stays local. Tagged `v0.1.0`.
- **Stopping point:** ★ release point.
- **Tracker:** #46.

### Releases

Milestones are named, not numbered by version. A release is a git tag `vX.Y.0` cut at a ★
release point; the first is `v0.1.0` at M5. Work after M5 gets new named milestones from a
new or superseding proposal.

### What this proposal does not order

The backlog (ghost-text autosuggest, multi-host over SSH) has no milestone until a proposal
places it. Multi-host needs a threat-model update first.

## Alternatives considered

- **Model first.** Train the student, then build the REPL around it. Rejected: defers the
  safety layer, and the REPL's prompt format must be fixed before training anyway (ADR-0009).
- **Version-numbered milestones** (`Milestone 0.1.0`, as the project template sets up).
  Rejected for now: before the first release, the milestones are risk steps, not versions.
  The template's release-tag scheme is kept.
- **M1 before M0.** Possible, since they are independent; M0 goes first because it retires
  the larger risk and produces session logs M2 can use.

## Graduation criteria

This proposal is implemented when M5 is closed and `v0.1.0` is tagged.

## Security impact

None directly. It places the safety layer first so that every later milestone builds on a
tested gate. Each milestone that adds an input (M2's teacher endpoint, M5's downloads)
updates the threat model.

## Backward compatibility

Not applicable before the first release.

## Open questions

- Whether M1 starts in parallel with M0 or after it.
- Whether M4 alone, without packaging, is worth a pre-release tag for early testers.
