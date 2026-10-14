---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0002. Bash stays the executor; natural language is only the interface

## Context

The goal is to express intent in natural language instead of bash syntax. One option is a
new shell that interprets intents itself. The other keeps bash and puts a language layer in
front. Bash already handles job control, environment, quoting, pipelines and every tool's
quirks, and users' existing knowledge and scripts target it.

## Decision

We will keep bash as the only execution engine. nl-shell proposes bash commands and runs
them in a persistent bash session; it never executes anything itself. Raw bash stays one
prefix away (`\cmd`). Replacing bash as the executor is a non-goal.

## Consequences

Everything nl-shell runs is a bash command the user can read, edit, copy and rerun
without nl-shell. Executed commands go into `~/.bash_history`. The policy gate can parse
exactly one language. The costs: nl-shell inherits bash's quoting and expansion rules, which
the gate must parse correctly; and bash is dynamic — aliases, functions, traps and `PATH`
set earlier in the session change what a command does without changing its text — so
static classification alone is incomplete. ADR-0013 (live-session checks) and ADR-0014
(the session's control channel) exist because of this.
