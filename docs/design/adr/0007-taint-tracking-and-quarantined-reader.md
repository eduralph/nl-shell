---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0007. Taint tracking and a quarantined reader for untrusted text

## Context

The model's context includes command output and file contents, which an attacker can
control: a README in a cloned repo, a web page fetched with `curl`, a file name. Text like
"ignore previous instructions and run …" in that output is prompt injection. Untrusted input
is not only the web.

## Decision

We will label every context item with a taint level:

- *trusted*: what the user typed, cwd, exit codes, paths the user typed;
- *local-reference*: `man`, `--help`, `apt-cache show`, the vendored tldr snapshot;
- *untrusted*: stdout, stderr, file contents, anything from the network.

If untrusted content was in context, the proposed command is forced to confirm, and sudo is
blocked unless the user typed it.

Long or raw untrusted text goes through a **quarantined reader** (the dual-LLM pattern, after
Google DeepMind's CaMeL paper): a separate model call that may only return schema-constrained
output, such as `{"flags": [...], "summary": "<=200 chars"}`. The command model never sees
the raw text. The reader runs in its own process with no PTY access, and the command model
has no network; the REPL is the only bridge and passes only schema-valid data.

## Consequences

Prompt injection through output can at worst get a command proposed, never run without a
confirm. The reader adds one model call when untrusted text must be understood. The reader
can share the command model's weights with a LoRA adapter or a different prompt, so it costs
little VRAM.
