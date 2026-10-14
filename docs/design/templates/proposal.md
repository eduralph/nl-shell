---
created: <DD.MM.YYYY HH:MM>
type: proposal
status: draft   # draft | accepted | implemented | withdrawn
author:
tracking-issue:
tags:
  - proposal
---
# Proposal: <title>

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

What problem does this solve? Why now? What does the user see?

## Design

The proposed change. Be concrete about the affected components (REPL, policy gate, PTY,
model backends, data pipeline, installer) and about any effect on the safety properties in
the threat model — those carry the highest bar.

## Alternatives considered

What else was weighed, and why this over those?

## Graduation criteria

How do we know it is done and correct? Which tests, which eval metrics (architecture §10),
and what definition of done?

## Security impact

Which untrusted inputs from `.oss-scanner/threat_model.md` does this touch? Does it add a
new one, or change what is trusted? Update the threat model in the same change if so.

## Backward compatibility

Effect on existing session logs, training data, configs and users' installs.

## Open questions

Mark unresolved points explicitly.
