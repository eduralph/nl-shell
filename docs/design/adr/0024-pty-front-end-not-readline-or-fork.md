---
created: 15.10.2026 10:45
type: adr
status: Proposed
tags:
  - adr
---
# 0024. nl-shell owns the session: a PTY front end, not a readline integration or a bash fork

## Context

ADR-0002 keeps bash as the executor, and ADR-0014 runs it in a PTY behind nl-shell's own
REPL. That shape has a cost: the input router (M0.7, later the M3 classifier), the control
channel and terminal ownership (ADR-0014), sudo passthrough (ADR-0015), and front-end hooks
that must be rebuilt for every further shell. Two other shapes keep bash as the executor
and avoid part of that cost. The design review of 2026-10-15 weighed both.

- **Readline integration.** A `bind -x` key binding (zsh: a ZLE widget; fish:
  `commandline`; PowerShell: PSReadLine) sends `READLINE_LINE` to the local model and
  replaces the buffer with the proposal; Enter confirms. It needs no router, because the key
  marks intent; no PTY; and no sudo passthrough. The user's terminal, completion and job
  control stay untouched, and each further shell costs a small adapter. But bash hands
  command output to no one, so the model loses output context, fix-on-failure, `?`
  explanations and the `@@` loop. The gate can only shape what lands in the buffer: Enter
  runs anything, and a proposal looks like typed text. Read-only commands cannot run
  without an Enter. The session runs inside the user's dotfiles (other `PROMPT_COMMAND`
  hooks, bash-preexec, ble.sh), which nl-shell cannot test against. Session logs lose
  the output.
- **Forking bash.** A fork would give the gate bash's own parser and a pre-execution hook
  in `execute_cmd.c` that sees every simple command after expansion. But bash is GPLv3, so
  the fork and anything linked into it would be GPLv3, against ADR-0012. Users would lose
  Ubuntu's security updates for bash unless the fork tracks upstream indefinitely, which
  weighs heavily for a tool that runs sudo. nl-shell would ship a second bash instead of
  the system one.
- **A loadable builtin** (`enable -f`) gives in-process hooks into stock bash without a
  fork. It is C linked against bash, which raises the same license question, and it adds
  nothing that ADR-0013's snapshot and ADR-0014's control channel need today.

## Decision

We will keep the PTY front end of ADR-0014. nl-shell owns the terminal, starts bash with
its own rcfile and control channel, and natural language is the default input.

We reject a readline integration as the primary interface. nl-shell's goal is to carry
out tasks with the user, which needs command output and an enforced gate, not only to
write one command.

We reject forking bash and will not ship a loadable builtin. The stock bash from the
target's package manager MUST stay the executor (ADR-0002, ADR-0023).

What a fork would have offered comes from existing mechanisms instead: tree-sitter-bash
and the live-session snapshot for classification (ADR-0013), and the fd-3 control channel
for command boundaries and exit codes (ADR-0014). The `extdebug` `DEBUG`-trap veto stays
the deferred alternative recorded in ADR-0013.

## Consequences

- The router stays. A misroute costs usability, not safety, because every route ends at
  the gate (ADR-0013). The heuristic needs hardening until the M3 classifier replaces it.
- The PTY, terminal-ownership and sudo-passthrough work (#101, #102, #13, ADR-0015) stays on the
  critical path of M0.
- Each further shell needs its own front-end hooks, parser and gate rules (ADR-0023,
  "Shells later"). A readline adapter is not a shortcut for that.
- A readline key binding MAY be added later as a secondary entry point that hands one
  proposal to the user's own shell. It would run outside nl-shell's gate and logs, so it
  needs its own ADR and a threat-model note before it ships.
- Reversing this decision would cost the REPL, PTY and passthrough code, but not the
  model, the training pipeline, the gate rules or the targets, which both shapes share.
