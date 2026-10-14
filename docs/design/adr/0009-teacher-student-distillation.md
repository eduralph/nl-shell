---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0009. Distil a small student from a large local teacher

## Context

A model that already knows Ubuntu 26.04 and answers in under a second on a 16 GB card must
be small (1.5–3B). Small general models do not know 26.04 specifics (sudo-rs, uutils,
Wayland-only, systemd-only). A large model knows more but is too slow for the target
hardware.

## Decision

We will use a large teacher model on the developer's own hardware to generate training
data, filter it by running every command in a sandboxed 26.04 VM, and fine-tune a 1.5–3B
student with QLoRA. The student is what ships. Training input uses the same format as the
REPL prompt (state block + intent).

## Consequences

The shipped model is fast and specific to 26.04. It needs a data pipeline (M2) and a
retraining loop for point releases (M4). The teacher's license must permit using its outputs
to train a distributed model, and the student base model's license must permit
redistributing fine-tuned weights; both are checked before the work that depends on them.
