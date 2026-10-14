---
created: 14.10.2026 20:48
type: adr
status: Proposed
supersedes: ADR-0005
tags:
  - adr
---
# 0013. Policy gate v2: a read-only allowlist checked against the live session

## Context

ADR-0005 classed commands from their text with a parser and a denylist. The 2026-10-14
design review (#63, #64) showed that this cannot keep the promise that nothing changes the
system without a confirm:

- **The session decides what a command does, not its text.** nl-shell runs one persistent
  bash. An earlier `source ./env.sh`, an alias, a function, a `PATH` change, a `DEBUG`
  trap, `enable -n`, `BASH_ENV` or `PROMPT_COMMAND` can make `du` or `ls` run anything.
  The user may have set these up via raw `\cmd` input or `~/.bashrc`, or a confirmed
  command may have done it. A parser sees none of this. ADR-0005 claimed aliases and
  functions "do not hide" a privileged step; that claim was wrong.
- **A denylist misses commands that look read-only but run code:** `PAGER=sh git log`,
  `git -c core.pager=sh log`, `less` with `LESSOPEN`, `man -P cmd`, `find -exec`,
  `awk 'BEGIN{system("…")}'`, `sed '1e cmd'`, `sort --compress-program=cmd`,
  `tar --to-command=cmd`, `ssh -o ProxyCommand=…`, `LD_PRELOAD=./x.so ls`,
  `cat </dev/tcp/host/80`, `ls > ~/.bashrc`, and command words that only become `rm`
  after expansion (`$'\x72m'`, `"r"m`, `$(echo rm)`).
- **bashlex** (last release January 2023) cannot parse `$((…))` and takes complex
  expansions literally. **tree-sitter-bash** is maintained and marks what it cannot parse
  with explicit `ERROR`/`MISSING` nodes.
- The documents disagreed on whether raw `\cmd` input goes through the gate.

## Decision

We will classify every command with the procedure in
[`specs/policy-gate/v1.md`](../specs/policy-gate/v1.md). In summary:

1. **Parser:** tree-sitter-bash, pinned. Any `ERROR` or `MISSING` node makes the command
   *unparseable*, which counts as mutating.
2. **Classes**, from lowest to highest: `read-only` < `mutating` = `session-altering` <
   `destructive` < `privileged`. A list, pipeline or compound command gets the highest
   class of its parts. `session-altering` behaves like `mutating`, plus a warning that it
   changes how later commands are classed.
3. **Read-only is an allowlist**, never the default. Each entry names a command and the
   options and operand shapes it may take. An unknown command, or a known one with an
   unlisted option, is at least `mutating`. Nothing that can reach the network is on the
   allowlist.
4. **Static rules** raise a command to at least `mutating`:
   - prefix assignments (`VAR=x cmd`);
   - any output redirection, except to `/dev/null` or between stdout and stderr (`2>&1`);
   - input redirection from anything other than an existing regular file;
   - `/dev/tcp` or `/dev/udp` anywhere;
   - command or process substitution, or arithmetic expansion, in command position;
   - a command word that isn't a plain literal after quote removal.

   The shell builtins and variables that change the session (`alias`, function
   definitions, `source`/`.`, `eval`, `exec`, `trap`, `enable`, `set`, `shopt`, and
   assignments to `PATH`, `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `PS0`–`PS4`, `LD_*`,
   `PAGER`, `EDITOR`, `VISUAL`, …) are `session-altering`.
5. **The live session is checked before anything runs unconfirmed.** The PTY session
   reports a snapshot after every command (ADR-0014): alias definitions, function names,
   disabled builtins, traps, `extdebug`, `PATH` and a hash of `PROMPT_COMMAND`.
   - **Aliases are expanded** using their definitions from the snapshot, as bash would, and
     the expansion is classed. This matters because Ubuntu's default `~/.bashrc` makes `ls`
     and `grep` aliases for `--color=auto`; without expansion, `ls` would always ask.
   - **Any other command word** must resolve, under the snapshot, to an allowlisted
     builtin or to an executable in a dpkg-owned system directory, with no function of
     that name. The session hooks must also be unchanged from nl-shell's baseline.

   Otherwise the command is at least `mutating`, and the confirm says why ("`du` is a
   function defined in this session"). If the snapshot is missing or nl-shell's hooks were
   changed, the session is *degraded*: everything needs a confirm until the session is
   restarted.
6. **Rejected outright** (cannot be confirmed, for proposals and typed input alike):
   - redirections to or from file descriptor 3 or higher (they would reach nl-shell's
     control channel, ADR-0014);
   - the sudo rules of ADR-0015.

   Model proposals containing C0 control characters or bidi or zero-width characters
   outside quotes are also rejected. Typed input with them is shown escaped instead.
7. **Everything goes through the gate, including raw `\cmd` input.** `\` skips only the
   intent classifier. Typed input is never *blocked* for taint reasons (ADR-0016), but it
   gets the same confirm as a proposal of the same class.
8. **The model's risk label can only raise the class**, never lower it (kept from ADR-0005).

Alternatives considered:
- **A run-time veto through bash's `extdebug` `DEBUG` trap**, which sees each simple
  command after alias and function expansion. Not adopted for v1: it competes with user
  traps, and a veto at run time can stop a pipeline halfway. Revisit if the snapshot check
  proves too coarse.
- **Sandboxing every command** with bubblewrap or Landlock. Rejected for now: nl-shell
  must act on the user's real system. It stays an option for running read-only commands.

## Consequences

- The gate's safety no longer rests on parsing alone. A command only runs unconfirmed if
  the allowlist, the static rules and the live session all agree.
- More commands ask for a confirm than under ADR-0005, especially early on while the
  allowlist is small. That is the intended cost. The allowlist grows through ordinary PRs,
  and each entry is a reviewable claim.
- The gate needs ADR-0014's control channel; the two are built together (#10, #12).
- `specs/policy-gate/v1.md` is the contract #12 tests against. A change to the allowlist
  or the rules is a spec change, and goes on the human-only review list in nl-shell-pdca.
