---
created: 14.10.2026 19:22
type: index
tags:
  - adr
---
# Architecture Decision Records

Numbered, immutable records of significant decisions and *why* they were made. An ADR is
never edited after acceptance; a later ADR supersedes it, and this index records the
supersession. Format follows the lightweight Nygard template (see ADR-0001).

ADRs 0002–0012 record the decisions `docs/plan.md` already makes. They start as Proposed and
are ratified in the Foundations milestone.

## Index

| # | Title | Status |
|---|-------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-bash-stays-the-executor.md) | Bash stays the executor; natural language is only the interface | Proposed |
| [0003](0003-locally-complete.md) | Locally complete: no cloud, no API keys, no telemetry | Proposed |
| [0004](0004-human-confirms-anything-mutating.md) | The human confirms anything that changes the system | Proposed |
| [0005](0005-deterministic-ast-policy-gate.md) | A deterministic, AST-based policy gate decides what runs | Proposed |
| [0006](0006-sudo-via-pty-passthrough.md) | sudo runs through PTY passthrough; the password never reaches the model | Proposed |
| [0007](0007-taint-tracking-and-quarantined-reader.md) | Taint tracking and a quarantined reader for untrusted text | Proposed |
| [0008](0008-llama-cpp-gguf-runtime.md) | llama.cpp with GGUF models as the one runtime across GPU vendors | Proposed |
| [0009](0009-teacher-student-distillation.md) | Distil a small student from a large local teacher | Proposed |
| [0010](0010-generic-training-data-local-personalization.md) | Ship a model trained on generic data only; personalize locally | Proposed |
| [0011](0011-python-and-prompt-toolkit.md) | Python and prompt_toolkit for the REPL | Proposed |
| [0012](0012-apache-2-and-dco.md) | Apache-2.0 license and DCO sign-off | Proposed |
