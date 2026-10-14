---
created: 14.10.2026 21:38
type: adr
status: Proposed
tags:
  - adr
---
# 0023. OS targets: one folder per OS, models referenced by manifest

## Context

nl-shell is built for Ubuntu 26.04, and that knowledge is spread across the design:

- The student's weights carry it. ADR-0009 fine-tunes a small model because general models
  do not know 26.04 details (sudo-rs, uutils, systemd-only).
- The policy gate's read-only allowlist (`specs/policy-gate/v1.md` §6) names commands and
  flags as they behave on 26.04, including `apt` and `dpkg`. Live-session resolution (§7.4)
  accepts only executables owned by a dpkg package.
- The training pipeline harvests man pages and `--help` from a 26.04 VM and filters every
  generated command by running it there. The eval split includes 26.04-specific probes.
- The prompts, the sudo handling (ADR-0015, checked against sudo-rs 0.2.13) and the gate's
  conformance vectors assume a default 26.04 session.

We want nl-shell to run on other operating systems later without a redesign: swap the
OS-specific parts as a unit, and keep each OS's model in the repository.

A model is 2–4 GB. GitHub refuses files over 100 MB, and Git LFS quotas are smaller than
one model, so the weights themselves cannot live in git. ADR-0020 already puts weights in
`$XDG_DATA_HOME/nl-shell/`.

Bash is also built into the design (ADR-0002): the proposal format, the gate's parser and
rules, the PTY hooks (ADR-0014) and the history file. Other shells are wanted later, but
not now.

## Decision

**A target is one supported OS release.** We will keep everything OS-specific for a target
in one folder in the repository, `targets/<id>/`, where `<id>` is the os-release `ID` and
`VERSION_ID` (e.g. `ubuntu-26.04`):

| File | Holds | Used by |
|---|---|---|
| `target.toml` | id, display name, os-release match, shell (`bash`), package-owner query (e.g. `dpkg -S`), sudo implementation and version checked | REPL, gate |
| `model.toml` | the model manifest (below) | installer, REPL |
| `allowlist.toml` | the policy gate's read-only allowlist for this OS | gate |
| `gate-vectors.toml` | the gate's conformance vectors for this OS's default session | gate tests |
| `prompts/` | OS-specific prompt pieces (the default `prompts.yaml` layer) | REPL, training |
| `harvest.toml` | what the pipeline harvests and the VM image it builds and filters in | training |
| `eval/` | the frozen eval split, including the OS-specific probes | training, release |

Code MUST NOT name an OS outside a target folder. A check that today is OS-specific reads
its value from the active target: the gate's allowlist, and the dpkg ownership test of
ADR-0013, which becomes the target's package-owner query. Spec `policy-gate/v1` is updated
with this ADR to say so.

**Models are referenced, not stored.** `model.toml` lists each model file the target ships
(the command model, which the quarantined reader shares per ADR-0017, and the intent
classifier once it exists) with:

- the download URL and size;
- the SHA-256 of the file;
- the base model, its license, and the training run (pipeline commit and dataset hash);
- the quantization, if a target offers more than one.

The installer downloads the weights into `$XDG_DATA_HOME/nl-shell/models/` and MUST verify
the SHA-256 before use. The REPL MUST refuse to load a weight file whose hash does not match
the active target's manifest. Downloads happen only at install or on an explicit update,
never at run time, so nl-shell still works with the network unplugged (ADR-0003). A local
personalization adapter (ADR-0010) is stored per target and base-weight hash, and is not
loaded on different weights.

**Choosing the target.** At start-up nl-shell reads `/etc/os-release` and picks the target
whose `ID` and `VERSION_ID` match exactly. It MUST NOT fall back to a similar target (via
`ID_LIKE` or a nearby version). With no match, nl-shell says so and starts only if the
user names a target (`--target <id>` or `target =` in `config.toml`). A target used on an
OS it does not match runs the gate in degraded mode: every command needs a confirm, because
its allowlist was never checked on that OS.

**A target is shippable** when its allowlist has been reviewed like any allowlist change
(spec `policy-gate/v1`, Versioning), its gate vectors pass, and its model meets the eval
thresholds on its own eval split. `ubuntu-26.04` is the first and, for now, only target.

**Shells later.** `target.toml` names the shell, and `bash` is the only accepted value. The
bash-specific parts (proposal format, gate parser and rules, PTY hooks, history file, router
builtins) stay as ADR-0002 and ADR-0013–0015 describe. Adding a shell is a later ADR; this
one only keeps the shell a field of the target so the folder layout does not change then.

## Consequences

Porting to a new OS becomes: add a folder, write its allowlist and probes, run the pipeline
against its VM, publish the weights, record their hash. The REPL, serving, the reader and
the language handling (ADR-0022) carry over unchanged.

The repository stays small, and each target folder fully describes its model, even though
the weights live elsewhere. The cost is a hosting place for the weights (a release asset or
a model hub) that must stay reachable for as long as a target is supported, and a release
step that records each new hash. A lost or changed file fails verification rather than
loading silently.

Each target carries its own allowlist and gate vectors, so safety review grows with every
target. A target cannot inherit another's allowlist, even a close one like Debian from
Ubuntu.

Follow-up work:

- the gate (#12) loads `allowlist.toml` and `gate-vectors.toml` from
  `targets/ubuntu-26.04/` instead of `src/nl_shell/policy/readonly.toml`.
- the installer (#48) and packaging (#50) must ship the runtime files of every target and
  download and verify weights.
- the training pipeline reads its harvest config, VM image and eval split from the target.
- architecture docs 05 (building blocks) and 07 (deployment) gain the target folder.
