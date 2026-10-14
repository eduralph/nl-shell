# Threat model: nl-shell

Reviewer-facing summary of what an attacker can reach and how bad each outcome is. The
design it describes is [docs/plan.md](../docs/plan.md). The project is **pre-code**: this
file describes the design, and each phase that lands code updates it to describe the code.
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
- **Command model**: a small local model (GGUF via llama.cpp) that proposes commands from
  the user's request plus a state block (cwd, last command, exit code, truncated output,
  filtered env).
- **Policy gate**: deterministic, AST-based (bashlex or tree-sitter-bash plus a denylist).
  Classes: read-only → runs directly; mutating → show and confirm; destructive → explicit
  confirm; privileged (`sudo`, `pkexec`, `su`, `doas`) → always confirm. The model's own
  risk label is a secondary signal only.
- **PTY bash**: one persistent session, so `cd` and `export` persist. On a `sudo` prompt the
  REPL switches to raw passthrough: keystrokes go straight to the PTY, nothing is logged,
  state capture is suspended.
- **Quarantined reader**: a separate model call that reads raw untrusted text and may only
  return schema-constrained output. The command model never sees the raw text.
- **Intent classifier**: a tiny local model that routes plain input to "bash" or "natural
  language".
- **Installer**: GPU detection (CUDA / ROCm / Vulkan / CPU), model download, install-time
  harvest of local man pages and `--help` for retrieval.
- **Training pipeline** (build time only, not shipped): data generation, sandbox execution
  filter, fine-tuning.

## Where untrusted input enters

Treat all of the following as attacker-controlled:

1. **Command output.** stdout and stderr of anything the shell runs: a `curl`, a `git log`
   of a cloned repo, a `cat` of a downloaded file, a package's post-install message. This
   text reaches the state block and the quarantined reader. It can carry prompt injection
   ("ignore previous instructions, run …"), terminal escape sequences, or text shaped like
   a REPL prompt or confirm dialog.
2. **File contents and file names** in the working directory: `ls` output, file names with
   newlines, quotes or shell metacharacters, and anything a command reads. A file name can
   end up inside a proposed command.
3. **Environment variables** captured into the state block, including values a cloned
   project's tooling (direnv, `.envrc`, a sourced script) set.
4. **The model's own output.** The command model's proposal is untrusted *as a command*:
   it can be wrong, it can have been steered by tainted context, and it can try to hide a
   privileged or destructive step (`bash -c "sudo …"`, an alias, `eval`, a here-doc,
   `$(…)`, a pipe into `sh`). Everything the policy gate does assumes the proposal is
   adversarial.
5. **Downloaded artifacts at install time**: model weights and any helper binaries. A
   tampered GGUF is a model the attacker chose.
6. **Network peers**, if the optional teacher fallback over LAN is enabled: the endpoint's
   responses are model output (item 4) from a host the user does not control end to end.

Trusted: what the user types at the REPL, the user's confirm/deny decisions, the cwd and
exit codes, paths the user typed, local reference docs (`man`, `--help`, `apt-cache
show`, the vendored tldr snapshot), and the config files in the user's home directory.

## What must hold

- **No mutating command runs without a confirm**, whatever the context held. If untrusted
  content was in context, every proposal is forced to confirm.
- **No privileged command runs unless the user confirms it**, and it is blocked entirely
  when the proposal came from tainted context and the user did not type `sudo` themselves.
  The check runs on the parsed AST, so wrappers (`bash -c`, `env`, `xargs`, `find -exec`,
  aliases, functions) do not hide it.
- **The sudo password never leaves the PTY passthrough**: it never reaches the model, the
  session log, `~/.bash_history`, the state block, or the `:context` view. The model may
  not emit `sudo -S` or pipe anything into `sudo`.
- **What the user confirms is exactly what runs.** The confirm UI shows the command as the
  shell will parse it; no hidden characters, escape sequences or trailing payloads.
- **Model output is never piped into the shell without going through the gate.**
- **The command model has no network.** The quarantined reader passes only schema-valid
  data back.
- **Locally complete**: no telemetry and no outbound connection in the default config.

## Components that matter most

In order: the policy gate (AST parsing, classification, the denylist) · the sudo
passthrough and PTY handling · taint tracking and the state block · the confirm UI and
output rendering (escape sequences) · the quarantined reader's schema enforcement · the
agent loop (`@@`, step limit, per-step confirm) · session logging and history write-back
(secret leakage) · the installer (download integrity, privilege use).

Lower priority: the intent classifier (a wrong route still goes through the gate), the
retrieval index, `:` meta-commands.

Out of scope: the training pipeline and its data (build time only, never shipped);
third-party code such as llama.cpp, bash, sudo-rs (report a dependency bug only if
nl-shell's use of it is what makes it exploitable).

## Known gaps: do not report these on their own

- **Nothing is built yet.** Until Phase 0 lands there is no code to attack; a patch is
  reviewed against the design above.
- **A model can be talked into proposing a bad command.** That is expected; the gate and
  the confirm exist because of it. A report is in scope only when a bad proposal gets past
  the gate or the confirm, or when tainted context is not marked as such.
- **The user can run anything they type with `\`.** Raw bash is the escape hatch by design.
- **A user who confirms a destructive command gets it.** The confirm is the control; in
  scope is a confirm that shows something other than what runs, or a mislabelled class.
- **The PTY session runs with the user's own rights.** There is no sandbox around the
  shell itself; the execution sandbox exists only in the training pipeline.

## How to exercise it

Placeholder until Phase 0. Once the REPL exists, this section names the test command, how
to start the REPL against a stub model that returns a fixed proposal (to test the gate
without a GPU), and how to replay a session log.

## How we rate severity

The project's promise is that the human stays in control of what runs, and that the sudo
password stays private.

- **Critical:** a mutating or privileged command runs with no confirm; the sudo password
  reaches the model, a log, the history or any file; the command that runs differs from
  the one the user confirmed; a tampered model or binary is installed without detection.
- **High:** untrusted content (command output, a file, a file name) steers a proposal
  without the taint forcing a confirm; a privileged step hidden in a wrapper is classed as
  non-privileged; the confirm UI can be spoofed or hidden by terminal escape sequences; the
  command model or reader gets network access in the default config.
- **Medium:** the agent loop exceeds its step limit or skips a per-step confirm; secrets
  from the environment reach the state block, the session log or `:context` despite the
  filter; a destructive command is classed as merely mutating.
- **Low:** information leaks in error messages; issues only reachable through a
  misconfiguration the user made; the intent classifier routing input to the wrong mode.

A clean reproducer matters more than a severity label. The best form is a failing test: a
command string the gate misclassifies, a fixed model proposal that gets past the confirm,
or a crafted command output that changes what is proposed.
