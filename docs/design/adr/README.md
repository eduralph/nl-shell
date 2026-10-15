---
created: 14.10.2026 19:22
type: index
tags:
  - adr
---
# Architecture Decision Records

Numbered, immutable records of significant decisions and *why* they were made. An ADR is
never edited after acceptance; a later ADR supersedes it, and this index records the
supersession. The only change ever allowed to an Accepted ADR is setting its status line to
`Superseded by ADR-NNNN`; a Superseded ADR is never edited at all, including the ones
superseded while still Proposed (0005–0007, 0011), while an ADR that is only partly
superseded and still Proposed (0008) stays editable like any Proposed ADR. Format follows
the lightweight Nygard template (see ADR-0001).

ADRs 0002–0012 record the decisions `docs/plan.md` already makes. ADRs 0013–0021 come from
the 2026-10-14 design review (#63) and supersede 0005–0007, 0011 and part of 0008, which were still Proposed. All
start as Proposed and are ratified in the Foundations milestone (#6).

**Building against Proposed ADRs:** code may implement an ADR while it is still Proposed;
it does not wait for ratification. The ADR stays the reference: if ratification changes or
supersedes it, the code is changed to match, not the other way round. A PR that implements a
Proposed ADR names it, so that change can be found.

## Index

| # | Title | Status |
|---|-------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-bash-stays-the-executor.md) | Bash stays the executor; natural language is only the interface | Proposed |
| [0003](0003-locally-complete.md) | Locally complete: no cloud, no API keys, no telemetry | Proposed |
| [0004](0004-human-confirms-anything-mutating.md) | The human confirms anything that changes the system | Proposed |
| [0005](0005-deterministic-ast-policy-gate.md) | A deterministic, AST-based policy gate decides what runs | Superseded by 0013 |
| [0006](0006-sudo-via-pty-passthrough.md) | sudo runs through PTY passthrough; the password never reaches the model | Superseded by 0015 |
| [0007](0007-taint-tracking-and-quarantined-reader.md) | Taint tracking and a quarantined reader for untrusted text | Superseded by 0016, 0017 |
| [0008](0008-llama-cpp-gguf-runtime.md) | llama.cpp with GGUF models as the one runtime across GPU vendors | Proposed; backend order superseded by 0019 |
| [0009](0009-teacher-student-distillation.md) | Distil a small student from a large local teacher | Proposed |
| [0010](0010-generic-training-data-local-personalization.md) | Ship a model trained on generic data only; personalize locally | Proposed |
| [0011](0011-python-and-prompt-toolkit.md) | Python and prompt_toolkit for the REPL | Superseded by 0021 |
| [0012](0012-apache-2-and-dco.md) | Apache-2.0 license and DCO sign-off | Proposed |
| [0013](0013-policy-gate-v2.md) | Policy gate v2: a read-only allowlist checked against the live session | Proposed |
| [0014](0014-pty-session-protocol.md) | PTY session protocol and terminal ownership | Proposed |
| [0015](0015-sudo-handling-v2.md) | sudo handling v2: passthrough only for privileged commands, behind a session-chosen prompt | Proposed |
| [0016](0016-taint-model-v2.md) | Taint model v2: provenance labels, per-class effects, clean-room re-derivation for sudo | Proposed |
| [0017](0017-quarantined-reader.md) | The quarantined reader: dual-LLM pattern, fixed-choice output, same weights | Proposed |
| [0018](0018-model-serving-and-wire-format.md) | Model serving and the proposal wire format | Proposed |
| [0019](0019-gpu-backend-order.md) | GPU backend order: CUDA on NVIDIA, Vulkan by default on AMD, ROCm opt-in | Proposed |
| [0020](0020-configuration-and-file-locations.md) | Configuration and file locations | Proposed |
| [0021](0021-python-prompt-toolkit-ptyprocess.md) | Python, prompt_toolkit and ptyprocess for the REPL (packaging left to #50) | Proposed |
| [0022](0022-english-input-first-replies-follow-locale.md) | English input first; replies follow the system locale | Proposed |
| [0023](0023-os-targets.md) | OS targets: one folder per OS, models referenced by manifest | Proposed |
