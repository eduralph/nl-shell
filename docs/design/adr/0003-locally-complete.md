---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0003. Locally complete: no cloud, no API keys, no telemetry

## Context

Existing natural-language shells mostly depend on hosted models. That sends the user's
commands, output and environment to a third party, needs an API key, and stops working
offline. A shell sees more private data than almost any other program.

## Decision

Every component of the shipped product MUST run on the user's own hardware and work with
the network unplugged:

- the command model, the intent classifier and the quarantined reader run locally;
- reference knowledge is local (man pages, `--help`, a vendored tldr snapshot), harvested
  at install time;
- the default config has no API keys, no telemetry and no cloud endpoints;
- an optional web lookup, if ever added, is off by default, quarantined (ADR-0007) and
  explicit per use.

The teacher model used during development runs on the developer's own machine over the LAN
and is not part of the shipped config.

## Consequences

No data leaves the machine by default, and the product works offline. The price is a
small model on consumer hardware (ADR-0009, ADR-0010), so quality depends on distillation and
local reference docs rather than on a large hosted model.
