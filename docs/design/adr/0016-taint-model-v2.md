---
created: 14.10.2026 20:48
type: adr
status: Proposed
supersedes: ADR-0007 (taint part)
tags:
  - adr
---
# 0016. Taint model v2: provenance labels, per-class effects, clean-room re-derivation for sudo

## Context

ADR-0007 labelled context items trusted, local-reference or untrusted. If anything
untrusted was in context, every proposal had to be confirmed, and sudo was blocked unless
the user typed it. The 2026-10-14 design review (#63, #67) found:

- **The model collapses.** `ls` of the cwd and the last command's output are in every
  state block and are always untrusted. So after the first command every proposal must be
  confirmed, and every sudo proposal is blocked: ADR-0004's two tiers become "always
  confirm". This also makes read-only diagnosis after a failure slow (the open item from
  #61/#62).
- **Taint is laundered back to trusted:**
  - executed commands go to `~/.bash_history` and come back as *trusted* few-shot examples;
  - reader output has no label, though it is derived from attacker text;
  - `local-reference` included `--help` output of anything on `PATH`, including binaries in
    `~/.local/bin` or a repository's `./bin`.
- Env and cwd were unlabelled or "trusted", though direnv sets env from a repository and
  directory names are attacker-chosen.

ADR-0013 changes the ground: read-only is now an allowlist of commands that can't write,
run programs or reach the network, checked against the live session. Read-only commands
are safe to run whoever suggested them; the worst they do is show local data on the
user's own screen and in the local model's context.

## Decision

1. **Labels by provenance** (who could have written the text):

| Item | Label |
|---|---|
| what the user typed; their confirm and edit decisions; exit codes from the control channel (ADR-0014) | trusted |
| cwd | trusted if every path component matches `[A-Za-z0-9._@+-]+`, else untrusted |
| man pages under `/usr/share/man` owned by a dpkg package; `--help` of dpkg-owned binaries (harvested in the reference VM, #23); the vendored tldr snapshot (#80) | local-reference |
| retrieved history examples (only turns recorded as untainted, point 4) | local-reference |
| stdout, stderr, file contents, file names, `ls` output, anything from the network | untrusted |
| environment variables, except a fixed list nl-shell itself sets or the shell defines (`HOME`, `USER`, `SHELL`, `LANG`, `TERM`, `PWD`) | untrusted |
| quarantined-reader output (ADR-0017) | untrusted |
| answers of `?` (ask mode) that read untrusted text | untrusted |

2. **Effects per class** (the class comes from the gate, spec `policy-gate/v1`):

| Class | Clean context | Untrusted item in this request's prompt |
|---|---|---|
| read-only | runs | runs |
| mutating, session-altering | confirm | confirm, marked "suggested after reading untrusted text", with the sources listed |
| destructive | explicit confirm | explicit confirm, marked as above |
| privileged | confirm | **clean-room re-derivation** (point 3), then confirm |

   "In this request's prompt" means the items nl-shell actually sent to the command model
   for this proposal, not everything the session has seen.
3. **Clean-room re-derivation for privileged proposals.** When a proposal is privileged
   and the prompt contained an untrusted item, nl-shell discards it, shows nothing, and
   asks the command model again with only trusted and local-reference items: the user's
   request, a clean cwd, the exit code, retrieved untainted history. Only the clean-room
   answer is shown, and it goes through the gate and confirm like any other proposal. The
   tainted privileged proposal is never shown or run, so text an
   attacker wrote can't introduce a privileged command; only the user's own request can.
   Typed input is never subject to this (ADR-0015 §8).
4. **Taint follows text across history.** The session log records each turn's taint (the
   labels of the items in its prompt). History retrieval (#79) skips tainted turns, and
   returns the rest as local-reference, never trusted.
5. **No decay by time.** The state block is rebuilt for every request from the current
   items, so old output drops out because it is no longer included, not because its
   label expires.

## Consequences

- Read-only diagnosis after a failure runs without a confirm again (`systemctl status x`,
  `journalctl -u x`), which closes the open item from #61/#62.
- Mutating commands keep their confirm, now with provenance shown, which makes confirm
  fatigue less likely to hide a steered command.
- A privileged request in a tainted context costs one extra model call. The previous
  rule would have blocked it.
- #11 implements the labels, #79 the history rule, #78 the clean-room path; ADR-0017
  covers the reader; architecture §8.2 and the threat model's "What must hold" are
  updated to match (#71).
