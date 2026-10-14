---
created: 14.10.2026 20:48
type: adr
status: Proposed
supersedes: ADR-0006
tags:
  - adr
---
# 0015. sudo handling v2: passthrough only for privileged commands, behind a session-chosen prompt

## Context

ADR-0006 put sudo in a PTY and switched the REPL to passthrough "on a sudo prompt". The
2026-10-14 design review (#63, #66) showed the prompt can be spoofed in both directions:

- **A missed prompt leaks the password.** `sudo -p 'Password: '`, `SUDO_PROMPT=…`,
  `sudo -A` with `SUDO_ASKPASS`, and the `su` and `pkexec` prompts don't look like
  "[sudo] password for". If the REPL misses the prompt, it captures the typed password
  into the output tail, the state block, `:context`, the session log and later training
  data.
- **A fake prompt steals the password.** `printf '[sudo] password for %s: ' "$USER"; cat`
  looks read-only under ADR-0005, would trigger passthrough, and reads what the user types.
- **Leaving passthrough** "when sudo finishes" was undefined, and printed text could fake it.
- **The credential cache:** after one confirmed sudo, later sudo calls on the same tty run
  without a password for the cache period, so the confirm is the only remaining control.
- ADR-0006 allowed a sudoers drop-in for "harmless" commands. That is a classic
  escalation path (`apt -o APT::Update::Pre-Invoke=…`, anything with a pager).

Facts checked on Ubuntu 26.04 (sudo-rs 0.2.13, `man sudo`, `sudo --help`):
- sudo-rs supports `-p/--prompt`, `-A/--askpass`, `-S/--stdin`, `-k` and `-v`;
- it honours the `SUDO_PROMPT` and `SUDO_ASKPASS` environment variables;
- `-p` overrides `SUDO_PROMPT`;
- without either, the prompt comes from PAM;
- `su` and `pkexec` are installed, `doas` is not.

ADR-0014 changed the ground under ADR-0006: input is never logged while a command runs,
and completion comes from a control channel that printed text can't reach.

## Decision

1. **Passthrough needs two conditions together:** the running command was classed
   `privileged` by the gate (spec `policy-gate/v1`), **and** the session's own sudo prompt
   (point 2) or, for `su` and `pkexec`, a PAM password prompt appears on the PTY. A
   command of any other class never enters passthrough, whatever it prints.
2. **A session-chosen prompt.** The rcfile exports
   `SUDO_PROMPT='[nl-shell sudo <random>] password: '`, with 16 random hex
   characters chosen per session. The gate rejects anything that would replace it: `-p`,
   `-A`, `-S`, and assignments to `SUDO_PROMPT` or `SUDO_ASKPASS` (spec §5).
3. **No keyboard sinks.** A read-only-looking command that would read the terminal (`cat`,
   `grep x` with no file operand at the start of a pipeline) is at least `mutating` (spec
   §2), so a fake prompt can't run unconfirmed.
4. **During passthrough**, capture stops, and the prompt line is removed from captured
   output together with the pwfeedback asterisks sudo-rs prints on it. Keystrokes are forwarded without logging (as for every running command,
   ADR-0014). Capture resumes on the next line sudo or the command writes after the
   password, or at the command's done record. Passthrough ends only at the done record
   on the control channel, never on printed text.
5. **Pipes into sudo are rejected:** a pipe, here-document or here-string into `sudo`,
   `su`, `pkexec` or `doas`. So are interactive root shells proposed by the model (`sudo
   -i`, `sudo -s`, `sudo su`, `sudo bash`, bare `su`). Typed input that opens a root shell
   runs in full passthrough with capture off until it exits, and the confirm says that
   nl-shell can't see or gate anything inside it.
6. **The credential cache is closed when it matters.** nl-shell runs `sudo -k` as a
   visible, logged housekeeping command when the REPL exits and when an `@@` loop ends.
7. **No sudoers drop-ins.** nl-shell ships none and its docs recommend none. No blanket
   `NOPASSWD`.
8. **"Typed by the user"** (ADR-0016) means the command text came from the user's own input
   line or their edit of a proposal, and was re-classed after the edit.

## Consequences

- A text-only attacker can neither make nl-shell miss a real prompt (it can't change the
  prompt) nor fake one (only privileged commands enter passthrough, and fake prompts
  can't run unconfirmed).
- The password still reaches only sudo, through the PTY with echo off.
- #13 implements points 1–4 against a fake `sudo` that reproduces the sudo-rs and PAM
  prompt strings (from #24), so it can be tested anywhere. #75 repeats the tests against
  real sudo-rs in the reference VM.
- Users who rely on their own `SUDO_PROMPT` lose it inside nl-shell. That is the price of
  point 2.
- `su` and `pkexec` prompts can't be set by nl-shell; point 1's class condition is what
  protects them.
