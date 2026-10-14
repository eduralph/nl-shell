---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - concepts
---
# 8. Cross-cutting concepts

> Living document. The core ideas the rest of the design is built on. Terms are also in the
> glossary (section 12).

## 8.1 The state block

What the command model knows about the session. Built fresh for every request:

- cwd, the last command, its exit code;
- the tail of its stdout/stderr, truncated per output (a token cap per command, from
  Butterfish) — long output goes to the quarantined reader instead;
- `ls` of the cwd;
- a filtered environment (rules **[OPEN]**, issue in M0);
- retrieved examples from the user's own `~/.bash_history` (ADR-0010).

The training input format is the same as this prompt (ADR-0009), so the student learns to use
exactly what the REPL gives it. `:context` shows the exact model input.

## 8.2 Taint

Every item in the state block carries a taint label (ADR-0007):

| Label | Examples | Effect |
|---|---|---|
| trusted | what the user typed, cwd, exit codes, paths the user typed | none |
| local-reference | `man`, `--help`, `apt-cache show`, the tldr snapshot | none |
| untrusted | stdout, stderr, file contents, anything from the network | proposal must be confirmed; sudo blocked unless the user typed it |

Taint is about **who could have written the text**, not whether it looks harmful. A clean
`ls` listing is still untrusted, because a file name is attacker-chosen.

## 8.3 The policy gate and risk classes

Every command, typed or proposed, is parsed into an AST and classed (ADR-0005):

| Class | Examples | Behaviour |
|---|---|---|
| read-only | `ls`, `du`, `systemctl status` | runs directly |
| mutating | `mkdir`, `apt install`, `git commit` | shown, then confirmed |
| destructive | `rm -r`, `dd`, `mkfs`, `chmod -R` | explicit confirm |
| privileged | `sudo`, `pkexec`, `su`, `doas` | always confirm |

The class is decided on the parsed AST, so wrappers (`bash -c`, `env`, `xargs`,
`find -exec`, aliases, functions) do not hide a privileged or destructive step. The model's
label can raise a class, never lower it. Unparseable means at least mutating.

## 8.4 Passthrough

The mode the REPL enters on a sudo prompt (ADR-0006): keystrokes go to the PTY unread, and
nothing is logged or captured until sudo returns. Interactive programs (`vim`, `htop`,
`less`) probably need the same full passthrough. **[OPEN]**, issue in M0.

## 8.5 The quarantined reader (dual-LLM pattern)

A second model call that reads raw untrusted text and may only answer in a fixed schema
(enums, flags, a short summary). The command model sees only that answer. The reader has no
PTY and no way to run anything. This limits prompt injection to what fits through the schema
(ADR-0007; Google DeepMind's CaMeL paper).

## 8.6 Input modes

| Input | Behaviour |
|---|---|
| plain input | Intent classifier: bash → run (gate applies); natural language → one proposal |
| `\cmd` | Force raw bash, skip the classifier |
| `?question` | Ask or explain, no execution |
| `@request` | Exactly one command, staged for review |
| `@@goal` | Multi-step loop, confirm per step, step limit |
| `:context`, `:history`, `:status` | Meta-commands |

Prefixes avoid `!` and `%`, which collide with bash history expansion and job specs.

## 8.7 Teacher and student

The **teacher** is a large model on the developer's hardware: it generates training data
and is a dev-only fallback. The **student** is the 1.5–3B model fine-tuned from that data;
it is what ships (ADR-0009). Training data is **grounded** (generated from a tool's real man
page) and **execution-filtered** (kept only if it ran correctly in a fresh 26.04 snapshot).

## 8.8 Ubuntu 26.04 specifics

What the student must know, and what it must unlearn:

- sudo-rs is `sudo`: no plugin system, `pwfeedback` on by default.
- uutils (Rust coreutils) provide the core utilities; `cp`, `mv`, `rm` remain GNU.
- Wayland-only GNOME 50: `wl-copy`/`wl-paste`, no `xclip`/`xdotool`.
- Kernel 7.0; last release with System V init compatibility → prefer systemd units.
- Unlearn: `apt-key`, `ifconfig`/`netstat`, sudo plugin options, X11 tooling.
- ROCm via `apt install rocm`.

## 8.9 Locally complete

Every runtime component works offline (ADR-0003). Lookup order: trained knowledge → local
`man`/`--help`/apt → (only if ever added, off by default) a quarantined web lookup.

## 8.10 Security

The threat model is [`.oss-scanner/threat_model.md`](../../../.oss-scanner/threat_model.md).
It lists the untrusted inputs, what must hold, the known gaps, and the severity scale the
PDCA security-review leaf rates patches against. A change that adds an input or moves a
trust boundary updates it in the same PR.
