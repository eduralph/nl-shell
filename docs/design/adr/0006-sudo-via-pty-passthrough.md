---
created: 14.10.2026 19:22
type: adr
status: Superseded by ADR-0015
tags:
  - adr
---
# 0006. sudo runs through PTY passthrough; the password never reaches the model

## Context

Users need `sudo` without leaving the REPL. The obvious shortcuts all leak the password:
`sudo -S` with the password piped in, a password stored by the REPL, or a blanket
`NOPASSWD` rule.

## Decision

We will run bash inside a PTY (`ptyprocess`/`pexpect`) so sudo prompts as in a normal
terminal. On a sudo prompt the REPL switches to raw passthrough: the keyboard goes straight
to the PTY, nothing is logged, and state capture is suspended. The prompt line is stripped
from captured output. Credentials are cached per tty by sudo itself (`sudo -v` once per
session).

The model MUST NOT emit `sudo -S` or pipe anything into sudo; the gate rejects it. There is
no blanket `NOPASSWD`; specific harmless commands may be allowed by a sudoers drop-in if
needed. Ubuntu 26.04 ships sudo-rs, whose prompt format and options are verified in M1.

## Consequences

The password exists only between the keyboard and sudo. A test proves it reaches no log,
history, state block or `:context` output. The prompt detection depends on sudo-rs's exact
behaviour, which must be re-checked on point releases.
