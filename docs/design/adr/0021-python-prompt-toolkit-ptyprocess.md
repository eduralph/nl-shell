---
created: 14.10.2026 20:48
type: adr
status: Proposed
supersedes: ADR-0011
tags:
  - adr
---
# 0021. Python, prompt_toolkit and ptyprocess for the REPL

## Context

ADR-0011 decided four things at once: the language, the line editor, the PTY library and
the packaging channel. The packaging part pre-decided what #50 was filed to research, and
can't be decided before the model-weights channel is known: multi-gigabyte weights can't
ship inside a `.deb`. Its consequences also named bashlex, which ADR-0013 replaced
(#63, #72).

## Decision

1. The REPL is written in **Python** (≥ 3.12) with **prompt_toolkit** for the line editor.
2. The PTY session uses **ptyprocess**, which can pass extra file descriptors to the child
   (`pass_fds`) as ADR-0014's control channel needs. pexpect is not required.
3. The command parser is **tree-sitter-bash** through its Python bindings (ADR-0013).
4. **Packaging is not decided here.** #50 decides it, together with the channel for the
   model weights, and the result is recorded in its own ADR.

## Consequences

- Each remaining decision can be accepted or replaced on its own.
- Python's startup time and the dependency on a Python runtime stay the costs. Revisit if
  REPL startup misses the latency targets (#81); the long-lived model server (ADR-0018)
  takes model loading out of REPL startup.
