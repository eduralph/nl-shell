---
created: 15.10.2026 12:00
type: adr
status: Proposed
tags:
  - adr
---
# 0025. Sensitive paths: reading credentials needs a confirm, and the output stays on screen

## Context

The read-only allowlist (ADR-0013, spec `policy-gate/v1` §6) includes `cat`, `head`,
`grep`, `env` and `printenv`, and read-only commands run unconfirmed even with untrusted
text in the prompt (ADR-0016). So a README that says "verify your key with
`cat ~/.ssh/id_ed25519`" can get the key read, printed, and put into the state block of the
next model request, the session log and later history retrieval. The threat model rates
untrusted text steering a secret into the model as High.

#20 decides how secrets are scrubbed from the environment and from captured output before
they are logged. That is content-based detection after the read: it can miss a key format,
and it does nothing to stop the read itself.

Which locations hold credentials depends on the OS and its default applications, so the
list belongs to the target (ADR-0023), not to the code.

## Decision

1. Each target lists sensitive locations in `targets/<id>/sensitive.toml` (ADR-0023): path
   prefixes, file-name patterns, and the environment dumpers. The file is reviewed like an
   allowlist change (spec `policy-gate/v1`, Versioning).
2. The gate resolves every file operand with `realpath` against the session cwd. A
   command is at least `mutating` when an operand, or a glob's directory part, matches a
   sensitive location, or when a recursive reader (`grep -r`, `cat` via `find -exec`,
   `tar`, `rsync`, `cp -r`) has an operand that is an ancestor of one. The confirm names
   the matched location. This applies to proposals and typed input alike (ADR-0013 point 7).
3. Output of such a command, and of the environment dumpers listed in `sensitive.toml`, is
   labelled `secret` (ADR-0016). Secret output is shown on screen only: it is never sent to
   the command model or the reader, never stored in the session log, and never retrieved
   as history.
4. Matching is by path, never by content. Content-based secret detection stays with #20 as
   a second layer.

## Consequences

- A text-only attacker can no longer get a credential file read unconfirmed, and a
  confirmed read still never reaches the model, the reader, the log or history.
- `env` and `printenv` stay read-only, so they still run without a confirm, but their
  output is never context. A request such as "why is my PATH wrong" then needs the user to
  name the variable, or a later, narrower dumper rule.
- Recursive reads over `~` (`grep -r token ~`) now need a confirm, because `~` is an
  ancestor of `~/.ssh`. A narrower path (`~/src`) stays read-only.
- Every target carries one more reviewed file, and a missed location is a gap until it is
  added. Path matching cannot see a credential stored somewhere unusual; #20's content
  filter is the backstop there.
- The gate checks the file system at gate time, and the command runs a moment later.
  Changing a path in between needs code running as the user, which is out of scope.
- #12 implements the rule and its vectors, #11 the `secret` label, #18 the session-log
  field.
