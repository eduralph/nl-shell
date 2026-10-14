---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0004. The human confirms anything that changes the system

## Context

A model proposing shell commands will sometimes be wrong, and can be steered by text it
reads (ADR-0007). Several existing tools offer an auto-execute mode. Fully autonomous
operation is a non-goal for nl-shell.

## Decision

We will run only read-only commands without asking. Every mutating, destructive or
privileged command MUST be shown and confirmed by the user first. There is no auto-execute
or "unsafe" mode (none of Butterfish's `!!`, `@@` or `gencmd -f`). The multi-step agent loop
(`@@`) confirms every step and has a step limit. Model output is never piped straight into
the shell.

## Consequences

The user stays in control of every change, which is the project's main promise (threat
model). Multi-step tasks are slower, since each step waits for a confirm. The classification
that decides "read-only" carries the safety property and must be deterministic (ADR-0005).
