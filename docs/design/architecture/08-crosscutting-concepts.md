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

Every item in the state block carries a taint label by provenance (ADR-0016):

| Label | Examples |
|---|---|
| trusted | what the user typed, their confirms and edits, exit codes from the control channel, a cwd made of plain path characters |
| local-reference | dpkg-owned man pages and `--help` (harvested in the reference VM), the tldr snapshot, retrieved *untainted* history |
| untrusted | stdout, stderr, file contents, file names, env (beyond a fixed list), reader output, anything from the network |

What the label changes depends on the command's class:

| Class | Clean prompt | Untrusted item in the prompt |
|---|---|---|
| read-only | runs | runs |
| mutating, session-altering, destructive | confirm | confirm, marked with the untrusted sources |
| privileged | confirm | **clean-room re-derivation**: the proposal is discarded and the model asked again with trusted and local-reference items only; only that answer is shown |

Taint follows text across history: the session log records each turn's taint, and
retrieval skips tainted turns.

Taint is about **who could have written the text**, not whether it looks harmful. A clean
`ls` listing is still untrusted, because a file name is attacker-chosen.

## 8.3 The policy gate and risk classes

Every command, typed or proposed (including raw `\cmd` input), is classed by the
procedure in [`specs/policy-gate/v2.md`](../specs/policy-gate/v2.md) (ADR-0027, which
supersedes ADR-0013):

| Class | Examples | Behaviour |
|---|---|---|
| read-only | `ls`, `du`, `systemctl status` — only what the allowlist names | runs directly |
| mutating | `mkdir`, `apt install`, `git status` (repository config can run commands) | shown, then confirmed |
| session-altering | `alias`, `source`, `export PATH=…`, function definitions | confirmed, with a warning that later commands are classed differently |
| destructive | `rm -r`, `dd`, `mkfs`, `chmod -R` | explicit confirm |
| privileged | `sudo`, `pkexec`, `su`, `doas` | always confirm |

Read-only is granted only inside a closed **grammar** — pipelines of simple commands made
of bare words or simply quoted strings, with four fixed redirect spellings, nothing bash
would expand — recognised without a parser library, so the words the gate sees are the
words bash runs. Inside the grammar, read-only is an **allowlist**: an unknown command or
an unlisted option makes it at least mutating. Before anything runs unconfirmed, every
command word is resolved against the **live session snapshot**: aliases are expanded (and
must land inside the grammar), and a function, a non-system executable or changed session
hooks make it at least mutating. Anything outside the grammar (`~`, globs, `$`,
assignments, compound commands, …) is at least mutating and is confirmed as written; the
gate does not try to understand it. A few things are rejected outright by a text scan
that over-approximates (sudo `-S`/`-p`/`-A`, pipes into sudo, any spelling of a
redirection to descriptor 3 or above). tree-sitter-bash is used only to word the confirm
(destructive, session-altering) and can fail without consequence. The model's label can
raise a class, never lower it.

### Confirm levels

What the user does to confirm depends on the class, so that confirming a destructive or
privileged command can't become the same reflex as confirming a `mkdir`:

| Class | Confirm | User action |
|---|---|---|
| mutating, session-altering | confirm | `y` then Enter; Enter alone is No |
| destructive | explicit confirm | type the first operand the command acts on (for `rm -r build`, type `build`), or `yes` when there is none; anything else is No |
| privileged | always confirm | as destructive, and the command is shown with its privileged part highlighted |

- The default answer is always No. Ctrl-C and Esc are No.
- Edit is always offered; an edited command is re-classed before any confirm (ADR-0015
  point 8).
- Inside `@@`, each step uses the level of its own class; the loop never lowers it.

Confirm-UI vectors (#74):

| Input at the confirm | Class | Result |
|---|---|---|
| Enter | mutating | not run |
| `y` Enter | mutating | run |
| `y` Enter | destructive (`rm -r build`) | not run |
| `build` Enter | destructive (`rm -r build`) | run |
| `yes` Enter | privileged (`sudo apt update`) | run |

## 8.4 Terminal ownership and sudo passthrough

While any command runs, keystrokes go to the PTY unread and unlogged, and output is
mirrored and captured (ADR-0014). Capture pauses while a program uses the alternate
screen, so `vim`, `less` and `htop` need no special mode.

**Sudo passthrough** (ADR-0015) additionally stops capture and strips the prompt line. It
starts only when the gate classed the running command privileged *and* the session's own
`SUDO_PROMPT` (random per session) or a PAM prompt for `su`/`pkexec` appears. It ends at
the command's done record on the control channel, never on printed text.

## 8.5 The quarantined reader (dual-LLM pattern)

A second model call that reads raw untrusted text and may only answer with fixed choices
and names that exist in the local dpkg/apt index; there is no free-text field (ADR-0017).
The command model sees only that answer, labelled untrusted. The reader has no PTY, no tools
and no network. This is the **dual-LLM pattern** (Willison, 2023) with constrained output,
not CaMeL: it limits prompt injection to the choice of an enum value and a few existing
names.

The answer's schema and its validation are in
[`specs/reader-schema/v1.md`](../specs/reader-schema/v1.md); `src/nl_shell/reader.py`
sends the request (its own prompt, `prompts/reader.yaml` in the target folder) and rejects
any answer that fails the schema, then drops names the local lookups don't confirm.

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

- sudo-rs is `sudo` (0.2.13 on 26.04, checked): no plugin system, `pwfeedback` on by
  default ([Phoronix](https://www.phoronix.com/news/sudo-rs-password-feedback)); supports
  `-p`, `-A`, `-S`, `SUDO_PROMPT` and `SUDO_ASKPASS`.
- uutils (Rust coreutils) provide the core utilities; `cp`, `mv`, `rm` remain GNU in 26.04
  and are planned to move to uutils in 26.10 ([LWN](https://lwn.net/Articles/1069481/)).
  The retrain loop (#44) has to cover that change.
- GNOME 50 is Wayland-only: prefer `wl-copy`/`wl-paste`. `xdotool` can't drive Wayland
  windows; `xclip` often still works through XWayland but isn't the 26.04 way.
- Kernel 7.0. Prefer systemd units. (The claim that 26.04 is the last release with System V
  init compatibility rests on secondary sources only; unverified.)
- Unlearn: `apt-key`, `ifconfig`/`netstat`, sudo plugin options, X11-only tooling.
- ROCm is available via `apt install rocm` (version 7.1.0), but AMD inference defaults to
  Vulkan (ADR-0019).

## 8.9 Locally complete

Every runtime component works offline (ADR-0003). Lookup order: trained knowledge → local
`man`/`--help`/apt → (only if ever added, off by default) a quarantined web lookup.

## 8.10 Security

The threat model is [`.oss-scanner/threat_model.md`](../../../.oss-scanner/threat_model.md).
It lists the untrusted inputs, what must hold, the known gaps, and the severity scale the
PDCA security-review leaf rates patches against. A change that adds an input or moves a
trust boundary updates it in the same PR.
