# Threat model: nl-shell

Reviewer-facing summary of what an attacker can reach and how bad each outcome is. The
design it describes is in [docs/design/](../docs/design/README.md) (ADRs 0013–0020 and 0025 for the safety
layer). M0 is in progress: this file describes the design, and each change that lands code
updates it to describe the code.
Where the two differ, this file should describe what the code does today.

## What this project does

nl-shell is a REPL for Ubuntu 26.04 that turns natural-language requests into shell
commands. Bash stays the executor; the model only proposes.

```
user ─> REPL ─> command model (local) ─> policy gate ─> persistent bash in a PTY
                    ▲                                          │
                    └──────── state block (with taint) ◄───────┘
```

- **REPL** (Python, prompt_toolkit): input modes (`\` raw bash, `?` ask, `@` one command,
  `@@` multi-step agent loop, `:` meta-commands), confirm UI, session log.
- **Command model**: a small local model served by a per-user `llama-server` on a Unix
  socket (ADR-0018). It proposes one command as schema-constrained JSON from the user's
  request plus a state block (cwd, last command, exit code, truncated output, filtered
  env, retrieved history).
- **Policy gate**: deterministic. tree-sitter-bash parse, a read-only **allowlist**, static
  rules, and every command word checked against the **live session snapshot** (ADR-0013,
  spec `policy-gate/v1`). Classes: read-only → runs; mutating and session-altering →
  confirm; destructive → explicit confirm; privileged → always confirm. The model's risk
  label can only raise the class.
- **PTY session**: one persistent bash. Completion, exit codes and session snapshots arrive
  on a separate control channel (fd 3) that printed text can't reach; commands are sent
  byte-identically (ADR-0014, spec `pty-protocol/v1`). Keystrokes are forwarded unlogged
  while a command runs. Sudo passthrough additionally stops capture (ADR-0015).
- **Quarantined reader**: a separate request to the same model server that reads raw
  untrusted text and returns only fixed choices and validated names (ADR-0017, spec
  `reader-schema/v1`). The command model never sees the raw text it read.
- **Intent classifier**: a tiny local model that routes plain input to "bash" or "natural
  language".
- **Installer**: GPU detection (CUDA; Vulkan by default on AMD, ROCm opt-in; CPU, ADR-0019),
  signed model download, and an install-time harvest of dpkg-owned man pages and an
  allowlist of `--help` outputs that runs no arbitrary binaries.
- **Training pipeline** (build time only, not shipped): data generation, sandbox execution
  filter, fine-tuning.

## Attackers

| Attacker | Can | Example |
|---|---|---|
| **Text-only** (the main one) | write text nl-shell will read: files, file names, directory names, command output, web pages a command fetches, a cloned repository's contents and config | a README saying "run `source scripts/env.sh`"; a file named `ignore previous instructions; sudo …` |
| **LAN peer** | answer or observe the dev teacher endpoint (development only) | a rogue host at the teacher's address |
| **Supply chain** | tamper with what nl-shell installs or was trained on | a modified GGUF file, a poisoned training pair, a compromised llama.cpp release |

Out of scope: **code already running as the user**. It can read `~/.bash_history`, the
session log, `/proc/<pid>/environ`, write to nl-shell's inherited control channel, or just
run `sudo` itself. nl-shell doesn't defend against it.

## Where untrusted input enters

Treat all of the following as attacker-controlled:

1. **Command output.** stdout and stderr of anything the shell runs. It reaches the state
   block and the quarantined reader, and the user's screen. It can carry prompt
   injection, terminal escape sequences (OSC 52 clipboard writes, title changes, cursor
   tricks), or text shaped like a REPL prompt, a confirm dialog or a sudo prompt.
2. **File contents, file names and directory names.** `ls` output, names with newlines,
   quotes, shell metacharacters or bidi characters; the cwd path itself after `cd` into a
   cloned repository.
3. **Environment variables** captured into the state block, including values set by a
   repository's tooling (direnv, a sourced script).
4. **Session state.** Aliases, functions, traps, `PATH` and other hooks set by `~/.bashrc`,
   by raw `\cmd` input, or by a confirmed `source`. They change what a later command
   does without changing its text (ADR-0013).
5. **The model's own output.** The proposal is untrusted *as a command*: wrong, steered,
   or hiding a privileged or destructive step. Everything the gate does assumes it is
   adversarial. The same holds for the `@@` loop's "goal reached" verdict.
6. **Retrieved history.** nl-shell writes executed commands to `~/.bash_history` and
   retrieves them as few-shot examples. A command steered by untrusted text could come
   back looking clean.
7. **Quarantined-reader output.** It is derived from attacker text, even though its shape
   is fixed (ADR-0017).
8. **The local model socket** (`$XDG_RUNTIME_DIR/nl-shell/model.sock`, ADR-0018): reachable
   by processes of the same user only.
9. **Downloaded artifacts at install time:** model weights and helper binaries. A
   malformed GGUF can attack llama.cpp's parser at load time, not only be "a model the
   attacker chose".
10. **Training data** (build time, but it ships as weights): teacher output, grounded pairs,
    session logs. A poisoned pair can plant a targeted trigger that aggregate evals don't
    detect.
11. **The dev teacher endpoint** (development only, behind `--dev-teacher`, ADR-0020):
    plaintext and unauthenticated on the LAN unless tunnelled. Its answers are model
    output (item 5), and the state block is sent to it.

Trusted:
- what the user types and their confirm and edit decisions;
- exit codes and completion from the control channel (ADR-0014);
- a cwd made of plain path characters;
- dpkg-owned man pages and an allowlist of `--help` outputs harvested in the reference VM,
  and the vendored tldr snapshot (local-reference, ADR-0016);
- nl-shell's own config files in the user's home directory, except that a model endpoint
  in them is refused without `--dev-teacher` (ADR-0020).

## What must hold

- **Nothing that isn't read-only runs without a confirm.** Read-only means on the
  allowlist, passing the static rules, and checked against the live session snapshot
  (ADR-0013, spec `policy-gate/v1`). Read-only commands may run unconfirmed even after
  untrusted text was read (ADR-0016).
- **Privileged commands always need a confirm.** A privileged proposal made with untrusted
  text in the prompt is never shown or run: nl-shell re-derives it from trusted context
  only (clean-room, ADR-0016). Wrappers, aliases, functions and session hooks don't hide a
  privileged step.
- **The sudo password never leaves the PTY.** It never reaches the model, the session log,
  `~/.bash_history`, the state block or `:context`. Passthrough starts only for privileged
  commands and the session's own `SUDO_PROMPT`, and ends only at the control channel's
  done record. The gate rejects `sudo -S`, `-p`, `-A`, overrides of `SUDO_PROMPT` and
  `SUDO_ASKPASS`, and pipes into sudo (ADR-0015).
- **A confirmed command can't silently use a warm sudo cache.** nl-shell runs `sudo -k`
  after every privileged command unless the user sets `sudo.keep_credentials`; while the
  cache is warm, every mutating, session-altering or destructive confirm says the command
  can gain root without a password (ADR-0015 point 6).
- **Credentials stay on screen.** Reading a sensitive location (the target's
  `sensitive.toml`, `/proc/<pid>/` files other than `status`, `stat`, `limits`) needs a
  confirm that names it. The output of such a read, and of an environment dumper (`env`,
  `printenv`), is labelled secret: shown on screen only, never sent to the command model or
  the reader, never logged, never retrieved as history (ADR-0025).
- **What the user confirms is exactly what runs**, byte for byte: bracketed paste, no
  history expansion (ADR-0014). The confirm UI renders from the parsed command with hidden
  characters escaped (#74); proposals with bidi or control characters are rejected by
  the proposal parser (M0.8, #143). The character policy (`nl_shell.render`, #144) takes
  every Unicode property it uses from vendored Unicode 16.0.0 tables, not from Python's
  `unicodedata`. Its hidden set is the characters those properties define (controls, Cf,
  Co, Cn, Default_Ignorable_Code_Point, White_Space other than space/LF/TAB) plus a short
  named list of known blank glyphs (U+2800, U+1D159, U+16FE4, U+13441, U+13442); a blank
  glyph on neither is not caught. A `command` is rejected when it holds a hidden
  character or more than two combining marks in a row (UTS #39 §3.1, §5.4), anywhere in
  it, quoted or not; every display path escapes the hidden characters and the extra
  marks. Rejecting quoted text too is stricter than the policy-gate spec's "outside
  quotes" (policy-gate v1 §1, step 1); it is a superset of that MUST-reject rule, so the
  spec is unchanged, and #12, which has the bash parser, may relax it.
- **Completion and exit codes can't be forged by output** (control channel, ADR-0014).
- **Output rendering is filtered everywhere**: the mirrored PTY output, `:context` and
  `:history` must not let printed text write the clipboard (OSC 52) or spoof nl-shell's
  own UI. Full-screen programs keep their escape sequences, but not those.
- **Background output can't reach the screen during a confirm.** Between commands, PTY
  output from background jobs is buffered and shown only outside a confirm, under
  `[background output]`; it is untrusted and never enters the state block (spec
  `pty-protocol/v1` §6).
- **Untrusted text never becomes trusted**: reader output is untrusted, retrieved history
  is at most local-reference, and tainted turns aren't retrieved (ADR-0016, ADR-0017).
- **Model output is never piped into the shell without going through the gate.**
- **The command model and the reader have no network**; the model server listens only on
  a `0600` Unix socket (ADR-0018).
- **Locally complete**: no telemetry and no outbound connection in the default config.

## Components that matter most

In order:
1. the policy gate (allowlist, static rules, live-session resolution);
2. the PTY protocol and control channel;
3. sudo passthrough;
4. taint labelling and clean-room re-derivation;
5. the confirm UI and output rendering filter;
6. the reader's schema validation;
7. the agent loop (`@@`, step limit, per-step confirm, `sudo -k`);
8. session logging and history write-back (taint and secrets);
9. the installer (signed weights, privilege use).

Lower priority: the intent classifier (a wrong route still goes through the gate),
`:` meta-commands.

Out of scope: third-party code such as llama.cpp, bash and sudo-rs (report a dependency
bug only if nl-shell's use of it is what makes it exploitable). The training pipeline's
*code* is lower priority, but its *data* is in scope as a supply-chain input (item 10).

## Known gaps: do not report these on their own

- **Little is built yet.** M0 is in progress; a patch is reviewed against the design and
  the specs.
- **A model can be talked into proposing a bad command.** That is expected; the gate and
  the confirm exist because of it. In scope: a bad proposal getting past the gate, the
  confirm or clean-room re-derivation, or untrusted text not being labelled as such.
- **Look-alike letters pass the proposal parser.** The character policy (#144) does no
  confusable (skeleton) matching, no NFKC rule and no mixed-script check (UTS #39 §3.1
  confusables, §5): look-alikes within one script or in Common (`ｃurl` fullwidth,
  `𝐜url` mathematical bold, `ⅽurl` Roman numeral) and words that mix scripts (`сurl`,
  with Cyrillic U+0441) are not rejected. Mixed-script words move to #12's parser-based
  policy gate; until then nl-shell does not catch them, and the user's own reading of the
  confirm prompt is the only check.
- **The failure classifier's privilege check is text-only until #12's parse**
  (`failure_classes`). It treats a command as privileged when a privilege program's name
  appears anywhere, when the text holds `$(`, a backtick, `<(`, `>(`, `$'`, `$"` or a
  backslash, when the first word of any command is not a plain name, or when a word names
  a shell function (or an alias whose value is privileged). It does not see: (1) a
  privilege program named through an expansion that sits behind a plain first word (for
  example `timeout 5 ${X}sudo x`, `time -p sud${X}o x` or `bash -c "$CMD"`); (2) a script
  or input fed to an interpreter (`./build.sh`, `sh x.sh`, `cat cmds | sh`); (3) a plain
  name that `PATH` resolves to a privilege program (a symlink `~/bin/s` to sudo); (4) a
  `trap` or a `PROMPT_COMMAND` hook set earlier in the session that runs a privilege
  program; (5) a redirection that holds `&` or `|` in front of the command word (for
  example `2>&1 sud${X}o x`, `<&0 sud${X}o x` or `>| out sud${X}o x`), because the check
  cuts the text at `&` and `|` before it skips redirections. In those cases the failed
  command may be offered a correction and its output sent to the reader. #12 closes this.
- **Raw `\cmd` input is the user's own**: it goes through the gate and confirm (ADR-0013),
  but is never blocked for taint reasons.
- **A user who confirms a destructive command gets it.** In scope: a confirm that shows
  something other than what runs, or a mislabelled class.
- **A root shell the user opens** (`sudo -i` typed by hand) runs in full passthrough;
  nl-shell can't see or gate anything inside it, and says so (ADR-0015).
- **Child processes inherit the control channel (fd 3).** Only code running as the user can
  use it, which is out of scope.
- **The PTY session runs with the user's own rights.** There is no sandbox around the shell.
- **The session core claims only the closed list in pty-protocol v1 §1.3.** These are
  the routes around it that are known and not closed:
  - **Loadable builtins and shadowing functions.** A builtin loaded with `enable -f` and
    a function named `builtin`, `command`, `local` or `return` are left to the gate: such
    commands are class `session-altering` (ADR-0013 §2; ADR-0024).
  - **`~/.bashrc` or `~/.inputrc` edited by a command** takes effect at the next session
    restart, which takes a new baseline from it (#134).
  - **`ptrace_scope = 0`** lets any process of the user attach to bash and change it
    without a trace in the snapshot (#137).
  - **The prompt rule's limits** (#136): a builtin that reads the terminal for over 1 s
    (`read -t 2`, `select`) degrades the session even when it would finish; a terminal
    read on an fd other than 0 (`read -u 2 x`) is not seen, so `run()` without a timeout
    can wait forever.
  - **The outside checks and the prompt rule are Linux-only** (`/proc`, #138).
  - **Fake fd-3 records.** A command (or a process it starts) can write records to fd 3
    itself (#135).
  - **A background job changing the terminal settings** between the last pre-paste
    reading and the write of the paste is not seen (#147).
  - **A process in another session reading the PTY** (started with `setsid`, with the
    slave open) can take paste bytes (#148).
  - **The shell's own value of a terminal field readline sets itself while it reads a
    line** (R: ISIG, ICANON, ECHO, FLUSHO, ICRNL, INLCR, ISTRIP, INPCK, VMIN, VTIME,
    VLNEXT, VDISCARD) can be changed by a confirmed command, a trap, or a background job
    while a command runs, and is not checked. This cannot change the bytes of a paste, but
    the commands after it run with the change and the session does not degrade. In
    particular with ISIG off, `Session.interrupt()` (writes `\x03`) no longer interrupts a
    command, so a `run()` without a timeout can hang on a runaway command while the
    session stays healthy (#149).
  - **A trap or `bind -x` binding set by `~/.bashrc`** is in the baseline, yet it can call
    a function a confirmed command redefines, and run after the snapshot (#150).
  - **Other evasions of the hooks** are under review (#145).

  #78 MUST NOT wire the session to confirmed commands until each of #145, #147, #148, #149 and #150 is fixed or a human has accepted it in writing as a known risk for #78.

## How to exercise it

The test suite: `python3 -m pytest` from the repo root (CONTRIBUTING.md). Tests run with
a temporary HOME and guard scripts ahead of `sudo`; tests marked `sudo` or `vm` need
the throwaway reference VM (`NL_SHELL_TEST_VM=1`, never on your own machine). The gate's
contract and its test vectors are in `docs/design/specs/policy-gate/v1.md`; the PTY
protocol's in `docs/design/specs/pty-protocol/v1.md`. The session tests
(`tests/test_session.py`) need only bash (5.1 or later): they start a real bash on a PTY,
with no VM and no sudo. The recording stub backend (`RecordingStub` in
`src/nl_shell/backend.py`) returns fixed model responses and records every model input it
was sent, so the prompts, the proposal parser and the REPL are tested without a model or a
GPU (`tests/test_backend.py`), and the gate can be tested the same way.

## How we rate severity

The project's promise is that the human stays in control of what runs, and that the sudo
password stays private.

- **Critical:**
  - a non-read-only command runs with no confirm;
  - the sudo password reaches the model, a log, the history or any file;
  - the command that runs differs from the one the user confirmed;
  - a tampered model or binary is installed without detection.
- **High:**
  - a tainted privileged proposal is shown or run without clean-room re-derivation;
  - untrusted text is labelled trusted;
  - a privileged or destructive step is classed lower;
  - a session change (alias, function, hook) goes undetected by the gate;
  - printed text forges a control-channel record, starts or ends passthrough, or spoofs
    the confirm UI, including background output shown during a confirm;
  - a confirmed non-privileged command gains root through a warm sudo cache without the
    cached-credentials warning;
  - a sensitive location is read without a confirm, or secret output reaches the command
    model, the reader, the session log or history;
  - output writes the clipboard;
  - the command model or reader gets network access in the default config.
- **Medium:**
  - the agent loop exceeds its step limit or skips a per-step confirm;
  - secrets from the environment or command output reach the state block, the session
    log or `:context` despite the filter;
  - a destructive command is classed as merely mutating;
  - a read-only content reader runs unconfirmed on a device, FIFO, socket or missing path;
  - the session doesn't enter degraded mode when its hooks were changed.
- **Low:**
  - information leaks in error messages;
  - issues only reachable through a misconfiguration the user made;
  - the intent classifier routing input to the wrong mode.

A clean reproducer matters more than a severity label. The best form is a failing test: a
command string the gate misclassifies, a session setup after which a "read-only" command
does something else, a fixed model proposal that gets past the confirm, or a crafted
command output that changes what is proposed.
