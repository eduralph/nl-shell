---
created: 14.10.2026 20:48
type: adr
status: Proposed; control channel transport superseded by ADR-0028
tags:
  - adr
---
# 0014. PTY session protocol and terminal ownership

## Context

nl-shell runs one persistent bash in a PTY (ADR-0002). Nothing decided how the REPL
learns that a command finished and with which exit code, or how prompt_toolkit and the
PTY share the user's terminal (#63, #65). Each obvious answer breaks a safety property:

- **Appending a sentinel** to the command (`cmd; printf 'DONE %s' $?`) means the command
  that runs is not the one the user confirmed. It also fails for `&`, here-docs and
  multi-line input.
- **Any marker in the output stream** can be printed by the command itself, or by a file
  it `cat`s. A forged marker would end capture early, report a false exit code to #62,
  or end sudo passthrough while sudo still reads keys.
- **Typing a command into readline** is not byte-transparent: `!` triggers history
  expansion, a literal TAB triggers completion, and a trailing `\` continues the line.
- Full-screen programs (`vim`, `less`, `htop`), resizing, Ctrl-C, Ctrl-Z and job control
  need a defined owner. #19 asked how interactive programs should run.

Facts checked on Ubuntu 26.04: bash 5.3 supports `PS0` (expanded after a command is read,
before it runs), and ptyprocess can pass extra file descriptors to the child
(`PtyProcess.spawn(..., pass_fds=...)`).

## Decision

The protocol is specified in [`specs/pty-protocol/v1.md`](../specs/pty-protocol/v1.md).
In summary:

1. **A separate control channel.** nl-shell starts bash with a pipe on file descriptor 3
   and its own rcfile: `bash --noprofile --rcfile <nl-shell rcfile>`. The rcfile sources
   the user's `~/.bashrc` first and installs nl-shell's hooks last. The hooks write
   records to fd 3 only:
   - `PS0` writes a *start* record when a command begins;
   - `PROMPT_COMMAND` writes a *done* record (exit status, `PIPESTATUS`) and a
     *session snapshot* (aliases, functions, disabled builtins, traps, `PATH`, stopped
     jobs) before each prompt.

   A `PROMPT_COMMAND` the user's `~/.bashrc` set (for example VTE's terminal integration)
   is kept and run after nl-shell's done record and before its snapshot; nl-shell sets
   `PS1` and `PS2` after the snapshot. Each record carries a sequence number, advanced
   in `PROMPT_COMMAND`: `PS0` runs its expansion in a subshell, so it can't advance a
   counter. Text a command prints goes to the PTY, never to
   fd 3, so it cannot forge a record. Redirecting to fd 3 is rejected by the gate
   (ADR-0013). A program that writes to its inherited fd 3 on purpose is code running as
   the user, outside the threat model.
2. **Exit codes and completion come only from the control channel.** A missing,
   out-of-order or malformed record puts the session into *degraded* mode: nl-shell sends
   it no further command, says why, and offers to restart the session.
3. **Byte-identical input.** The rcfile turns history expansion off (`set +H`). nl-shell
   sends the confirmed command as one bracketed paste followed by Enter, so readline
   inserts it literally: no completion, no expansion, newlines kept. The gate rejects
   commands containing the paste-end sequence or other C0 control characters, so the
   paste can't be ended early.
4. **Terminal ownership.**
   - At the prompt, prompt_toolkit owns the terminal.
   - From sending the command to the done record, nl-shell puts the terminal in raw mode
     and **forwards every keystroke to the PTY without logging it**. It mirrors PTY output
     to the screen and captures it under the capture rules.
   - Window-size changes are applied to the PTY. Ctrl-C and Ctrl-Z travel as bytes, and
     the PTY's line discipline signals the foreground job as in any terminal. Stopped jobs
     appear in the snapshot.
   - Capture pauses while a program uses the alternate screen and during sudo passthrough
     (ADR-0015). `vim`, `less` and `htop` therefore need no detection and no special
     mode, which answers #19.
5. **History.** The PTY bash does not write its own history (`HISTFILE` is unset in the
   rcfile). nl-shell appends each executed command to `~/.bash_history` itself, and
   records its taint in the session log (ADR-0016). This avoids duplicates and keeps the
   taint information.

## Consequences

- Completion detection is exact and can't be forged by output, and "what you confirm is
  what runs" holds at the byte level.
- The password is never in a logged input stream: input isn't logged while a command
  runs, and sudo-rs turns echo off. ADR-0015 handles the remaining risk, a program that
  echoes its input.
- The snapshot is what lets ADR-0013 check the live session without running extra visible
  commands.
- User rc files that rewrite `PROMPT_COMMAND` or `PS0` after the rcfile runs, or later in
  the session, push the session into degraded mode instead of silently weakening it.
- #10 implements this, #19 is answered by it, and #62 may trust exit codes only from
  done records.
