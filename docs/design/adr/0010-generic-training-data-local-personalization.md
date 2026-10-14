---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0010. Ship a model trained on generic data only; personalize locally

## Context

Shell history and session logs are the best training data, and also the most private:
they contain paths, hostnames and private infrastructure. The developer's own history is
available during development.

## Decision

The shipped model is trained only on generic 26.04 data: grounded man-page pairs, tldr,
correction pairs and synthetic sessions. The developer's own shell history and session logs
MUST NOT go into the shipped dataset; they may be used for the eval split and the intent
classifier only.

Personalization happens on the user's machine: retrieval of the user's own `~/.bash_history`
as few-shot examples (on by default, no training), and an optional local QLoRA on the user's
own logs (opt-in). That data never leaves the machine.

## Consequences

No one's private data is in the weights. Personalization quality depends on retrieval
until a user opts into local fine-tuning.
