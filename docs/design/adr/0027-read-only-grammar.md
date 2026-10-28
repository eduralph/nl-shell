---
created: 28.10.2026 17:45
type: adr
status: Proposed
supersedes: ADR-0013
tags:
  - adr
---
# 0027. Read-only is granted only inside a closed grammar; everything else is confirmed unparsed

## Context

ADR-0013 made read-only an allowlist checked against the live session, and that part has
held up. What has not held up is the assumption underneath its step 1: that the gate
parses *arbitrary* bash text with tree-sitter-bash and then reasons about the tree. The
safety property ("nothing runs unconfirmed unless proven read-only") is only as sound as
the claim that the tree means what bash means, and for arbitrary input that claim has to
be proven for the whole language.

The shell layer merged in #203 shows what that proof costs. Besides the walk itself it
carries a list of features where tree-sitter-bash and bash disagree (`<<-` here-docs, two
here-docs on one line, `[ x > y ]` read as a test instead of a redirection, extglob,
backslash-newline, bash 5.3 `${ }`), byte-level scans of the source to detect them, a rule
that a relative redirect target may name descriptor 3 when the cwd is `/dev/fd`, and a
budget of fidelity checks per node kind. Each item is correct and each was found by
review, which is the problem: the list is open-ended, every parser release can extend it,
and every entry became an issue with sub-issues. The differential fuzz test against shfmt
that the spec requires exists to find the next entry. The backlog is growing because the
task is unbounded, not because the work is behind.

Two facts bound the task if we let them:

- The gate makes one decision that matters: *read-only or not*. Every other class leads
  to a confirm, where the user reads the command text themselves. Whether a confirmed
  command is labelled `mutating` or `destructive` changes the wording and the taint
  effects of ADR-0016, not whether it runs. Misreading a confirmed command is a UX defect;
  misreading an unconfirmed one is the threat model's failure case.
- A read-only command never needs bash's full language. `ls -la /etc`, `grep -n foo
  file`, `journalctl -u ssh -n 50 | grep -i error`, `systemctl status nginx 2>&1` are
  words, pipes and two redirections. Expansions, substitutions, here-docs, compound
  commands, functions and prefix assignments are exactly the constructs that make text
  hard to read for bash and parser alike, and no entry on the allowlist needs them.

The sound way to make a decision procedure total over an infinite language is to refuse
to decide most of it. Related: ADR-0002 (bash stays the executor), ADR-0004 (the human
confirms anything that changes the system), ADR-0014 (fd 3 is the control channel),
ADR-0015 (sudo), ADR-0016 (taint effects per class), ADR-0025 (sensitive paths).

## Decision

1. **Read-only is granted only to commands in a closed grammar**, the *read-only grammar*,
   defined normatively in spec `policy-gate/v2`. Its shape:

   - a list of pipelines joined by `|`, `&&`, `||` or `;` — no `&`, no newline, no
     subshell, no group, no compound command, no function definition;
   - a pipeline element is one simple command: words, then redirections — no prefix
     assignment;
   - a word is a *bare word* from a closed ASCII character class (letters, digits,
     `_ . / : @ % + , = -`; the command word additionally without `=`), or a single-quoted
     string with no newline, or a double-quoted string containing none of `$`, `` ` ``,
     `\`, `!`. Nothing else: no `~`, no glob, no brace, no `$`, no backtick, no `#`;
   - a redirection is exactly one of `2>&1`, `>/dev/null`, `2>/dev/null`, `&>/dev/null`,
     or `<` followed by one word.

   A recogniser for this grammar is a few dozen lines, needs no parser library, and is
   sound by construction: for a string the grammar accepts, bash performs no expansion
   and no quote removal beyond stripping the quote characters, so the words the
   recogniser produces are the words bash runs. Its tests compare its word split against
   bash itself; there is no second parser to differ from.

2. **A command the grammar does not accept is at least `mutating`**, with the reason
   "outside the read-only grammar", and is shown to the user for confirmation exactly as
   written. The gate MUST NOT try to prove such a command read-only by any other means.
   This replaces ADR-0013's rule that an unparseable command is `mutating`: *unanalysed*
   is now the normal case, not the failure case.

3. **The rest of ADR-0013 stands and applies to grammar-accepted commands**: the class
   ladder; the allowlist of command words with their options and operand shapes; live
   session resolution (alias expansion — whose result MUST itself be re-checked against
   the grammar — functions, disabled builtins, `PATH` resolution to a package-owned
   system binary, unchanged hooks and pager variables); sensitive paths (ADR-0025); the
   model's risk label raising but never lowering the class; everything through the gate,
   including raw `\cmd` input.

4. **Rejections and the `privileged` class are decided by a text scan, not by a parse.**
   The outright rejections (descriptors 3 and above, `sudo -S`/`-A`/`-p`, pipes and
   here-docs into `sudo`, `SUDO_PROMPT`/`SUDO_ASKPASS`, forbidden characters in proposals,
   root shells in proposals) and the `privileged` marker (`sudo`, `su`, `pkexec`, `doas`,
   `run0`, `machinectl`) MUST be detected by patterns over the raw text, outside
   single-quoted runs, that over-approximate: anything that *could* be the construct
   counts. A false positive costs the user a rewrite of a rare command (`grep '3>' f`
   needs its quotes); a false negative is the control channel or the password prompt. The
   scan is deliberately crude so that it has nothing to get wrong.

5. **tree-sitter-bash becomes advisory.** The tree may be used to word the confirm
   (naming the destructive construct, the session-altering builtin, the wrapper's inner
   command) and to raise a class above `mutating` for ADR-0016's effects. It MUST NOT
   grant `read-only` and MUST NOT be the sole detector of a rejection. A parse that
   fails, hits a budget, or meets a construct the walker does not handle yields
   `mutating` with no reason; it is no longer a `GateFailure` that refuses the command.
   Consequently the fidelity checks of #203 (the untrusted-feature list, the here-doc and
   bracket byte scans, the descriptor-via-cwd rule) and the differential test against
   shfmt are no longer required and are removed; shfmt leaves the toolchain.

6. **Extending the grammar is a spec change.** `~`, globs and `$HOME`-style variables are
   the known candidates, each requiring gate-side expansion against the snapshot's cwd
   and `HOME` so that sensitive-path checks see the resolved operands. They are added
   only when the confirm they currently cost is shown to be frequent, through the
   human-only review of nl-shell-pdca, one construct per change. Until then the model is
   trained (M3) to express read-only tasks inside the grammar — absolute or cwd-relative
   paths, no globs — which is a training-data constraint, not a model capability.

Alternatives considered:

- **Finish the proof**: keep parsing all of bash and keep extending the fidelity checks
  until the differential test is quiet. Rejected: the list has no closing condition, each
  tree-sitter-bash release reopens it, and every entry is safety-critical and so P0/P1.
- **Sandbox read-only commands** (bubblewrap/Landlock) so a misclassification is contained.
  Still an option (ADR-0013 kept it), but it does not remove the parsing question; it
  changes the blast radius. May be layered on later for the grammar-accepted set.
- **Make the model emit structured argv instead of a command string** (`proposal/v1`
  carrying `argv` and redirections rather than text). Rejected for now: the proposal
  format is also the training target and the `\cmd` path types text anyway; the grammar
  achieves the same effect — a closed, parseable form — for both inputs without a v2 of
  the proposal format. Revisit if the grammar needs to grow past what a recogniser keeps
  readable.
- **A run-time veto via the `DEBUG` trap** after expansion. Rejected as in ADR-0013.

## Consequences

- The safety-critical surface shrinks from "all of bash, faithfully" to a grammar that
  fits in a page and a text scan that is allowed to be wrong in one direction only.
  Parser fidelity bugs become confirm-wording bugs, which ADR-0026 rates P2/P3 instead of
  P0.
- More confirms than ADR-0013 intended, for commands that use `~`, globs or variables
  until point 6 admits them. That is the accepted cost; the confirm shows the command
  verbatim and costs one keypress.
- Spec `policy-gate/v1` is replaced by `v2`: §1 step 2 and §2's parse-based rules are
  restructured around the grammar; §4's destructive rules move to the advisory layer;
  §5's rejections become text patterns; the conformance vectors keep their expected
  classes except that rows which relied on parsing to reach `read-only` (`du -sh ~/*`)
  become `mutating` until the grammar admits `~` and globs. The differential-parser
  requirement is dropped.
- Code: `gate/parse.py` is split. A new recogniser and a new text scanner carry the
  safety property; the tree-sitter walker is reduced to what the confirm wording needs
  and loses its fidelity machinery and `GateFailure`. Open issues whose purpose was
  parser fidelity are closed against this ADR rather than finished.
- Training (M3) gains a hard constraint: read-only examples are generated inside the
  grammar, and the eval runner (#81) reports how often a proposal for a read task falls
  outside it — that number, not review intuition, drives point 6.
- Reversal cost is low: the allowlist, the live-session check and the sensitive-path
  rules are untouched, and the tree-sitter walker still exists if a later ADR wants to
  grant read-only to more than the grammar.
