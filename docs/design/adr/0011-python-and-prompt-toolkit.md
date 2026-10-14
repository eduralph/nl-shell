---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0011. Python and prompt_toolkit for the REPL

## Context

The REPL needs a line editor with history and key bindings, PTY control, and access to the
parsing and model libraries the gate and the backends use.

## Decision

We will write the REPL in Python with prompt_toolkit for the line editor and
`ptyprocess`/`pexpect` for the PTY. Packaging favours pipx/uv or a `.deb` over a strict snap,
since the PTY and sudo need classic access.

## Consequences

Fast to build, with mature libraries for every piece (bashlex, tree-sitter bindings,
llama.cpp bindings). Startup time and the Python dependency on the user's machine are the
costs; revisit if startup time misses the latency targets.
