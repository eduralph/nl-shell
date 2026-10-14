---
created: 14.10.2026 19:22
type: adr
status: Superseded by ADR-0013
tags:
  - adr
---
# 0005. A deterministic, AST-based policy gate decides what runs

## Context

Something must decide whether a proposed command is read-only, mutating, destructive or
privileged. The model can label its own proposal, but a model's label can be wrong or
steered. Plain string matching misses commands hidden in wrappers (`bash -c "sudo …"`,
`env`, `xargs`, `find -exec`, aliases, functions).

## Decision

We will classify every command with a deterministic gate that parses it into an AST
(bashlex or tree-sitter-bash) and applies a denylist, into four classes:

| Class | Behaviour |
|---|---|
| read-only | runs directly |
| mutating | shown, then confirmed |
| destructive (`rm -r`, `dd`, `mkfs`, `chmod -R`, …) | explicit confirm |
| privileged (`sudo`, `pkexec`, `su`, `doas`) | always confirm |

The model's own risk label is only a secondary signal: it can raise the class, never lower
it. A command the gate cannot parse is treated as at least mutating. Tainted context forces a
confirm on every class (ADR-0007).

## Consequences

The safety decision is testable with a table of command strings, which is the most
important test suite in the project. The gate will sometimes over-classify and ask for a
confirm that was not needed; that is the accepted cost. Every bash construct the parser
cannot handle is a gap to track.
