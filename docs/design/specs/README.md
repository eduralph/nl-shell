---
created: 14.10.2026 19:22
type: index
tags:
  - spec
---
# Specifications

Normative, versioned contracts (see `../README.md`). None yet.

Candidates, both from [`docs/plan.md`](../../plan.md) §Data Formats:

- **Session log** (`session-log/v1.md`): one JSONL record per REPL turn. Written by the
  REPL (M0), read by the data pipeline (M2).
- **Training pair** (`training-pair/v1.md`): written by the data pipeline (M2), read by
  training (M3) and the eval suite (M4).

Write each as a spec, from `../templates/spec.md`, before a second consumer depends on it.
