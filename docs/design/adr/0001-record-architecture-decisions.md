---
created: 14.10.2026 19:22
type: adr
status: Accepted
tags:
  - adr
---
# 0001. Record architecture decisions

## Context

nl-shell makes a number of decisions early that later work depends on: what the model
may do, where the sudo password may go, what runs locally. Without a record, the same
questions come back in every issue, and the reasons are lost when the code changes.

## Decision

We will record significant decisions as Architecture Decision Records in
`docs/design/adr/`, using the Nygard-style template in `docs/design/templates/adr.md`.

- Files are numbered `NNNN-short-title.md` and indexed in `adr/README.md`.
- An ADR starts as Proposed. Once Accepted it MUST NOT be edited; a later ADR supersedes it,
  and the index records the supersession.
- A decision is "significant" when reversing it would change a safety property in the threat
  model, the shipped artifact, or more than one component.

## Consequences

Decisions and their reasons survive code changes. Reopening a settled question needs a
new ADR, which costs more than an issue comment; that is intended. A docs CI check enforces
immutability (Foundations milestone).
