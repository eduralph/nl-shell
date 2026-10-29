---
created: 14.10.2026 19:22
type: index
tags:
  - spec
---
# Specifications

Normative, versioned contracts (see `../README.md`).

| Spec | Version | Status | Decision |
|---|---|---|---|
| [Policy gate](policy-gate/v2.md) | v2 | draft | ADR-0027 (v1: ADR-0013, superseded) |
| [PTY session protocol](pty-protocol/v1.md) | v1 | draft | ADR-0014 |
| [Command proposal](proposal/v1.md) | v1 | draft | ADR-0018 |
| [Quarantined reader output](reader-schema/v1.md) | v1 | draft | ADR-0017, ADR-0018 |
| [Session log](session-log/v1.md) | v1 | draft | ADR-0014, ADR-0016, ADR-0020 |

Further candidates:

- **Training pair** (`training-pair/v1.md`): written by the data pipeline (M2), read by
  training (M3). Its first draft is in the frozen [`docs/plan.md`](../../plan.md) §Data Formats.
- **Eval** (`eval.md`): metric definitions and the eval runner's contract (#81).
- **Prompts** (`prompts/v1.md`): the editable prompts YAML and what `:context` shows (#16).

Write each as a spec, from `../templates/spec.md`, before a second consumer depends on it.
