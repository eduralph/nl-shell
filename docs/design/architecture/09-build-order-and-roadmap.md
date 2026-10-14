---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - roadmap
---
# 9. Build order and roadmap

> Living document. Summary of [proposal 0001](../proposals/draft/0001-implementation-arc.md),
> which holds the full detail. GitHub milestones carry the same names.

## The order

Safety layer first, model second. M0 builds the whole REPL and every safety mechanism against
the teacher; nothing after M0 changes what the gate, passthrough or taint do, only which
model proposes.

| Milestone | Proves | Stopping point |
|---|---|---|
| Foundations | Design docs, founding ADRs, threat model, docs CI | — |
| M0 — REPL prototype | The safety layer works: gate, confirm, passthrough, taint, logging | ★ yes: a usable dev tool with the teacher |
| M1 — 26.04 reference system | A reproducible 26.04 source of truth and sandbox | soft |
| M2 — Data pipeline | Verified, grounded training data; frozen eval split | soft |
| M3 — Student model and intent classifier | A small model and router trained for 26.04 | soft |
| M4 — Swap and evaluate | The student meets the eval targets fully offline | soft |
| M5 — Packaged release | Anyone with a 16 GB GPU can install it | ★ release point, `v0.1.0` |

## Where to start contributing

M0 issues labelled `good first issue`, and the policy-gate test table (M0.4): every command
string that should or should not need a confirm is a useful contribution.

## Later (backlog, no milestone)

Ghost-text autosuggest; multi-host support over SSH (needs a threat-model update first).
