# NL Shell: Natural-Language Shell for Ubuntu 26.04

> **Historical — frozen on 2026-10-14.** This is the original plan the project grew from. It
> is no longer maintained and already differs from the decisions in places (for example the
> reader's network access, the teacher as a user-facing fallback, the policy gate's
> denylist, and QLoRA on AMD). The live design is in [`design/`](design/README.md): the
> architecture overview, the ADRs (especially 0013–0020 from the 2026-10-14 design review,
> #63) and the specs. The milestones follow proposal 0001.

Experiment: replace bash *as the interface* with a locally distilled language model, while bash remains the executor underneath. Goal: express intent in natural language (including sudo operations) without leaving the REPL, with a model that already knows Ubuntu 26.04 and needs few or no lookups.

## Goals

- Natural-language REPL that proposes and runs shell commands in a persistent session
- sudo without leaving the REPL; the password never reaches the model, logs or history
- Model natively familiar with Ubuntu 26.04 LTS (sudo-rs, uutils, Wayland, systemd-only)
- Fast local inference (sub-second) via a small distilled student model
- External/untrusted information isolated from the command-proposing model
- **Deliverable: installable by anyone with a 16 GB NVIDIA or 16 GB AMD Radeon GPU on Ubuntu 26.04, no training required**

## Principle: Locally Complete

Every component runs on own hardware. The shell must work with the internet unplugged.

- Command model, intent classifier, quarantined reader: local (student on the desktop; teacher on the Framework via LAN as an optional fallback)
- Reference knowledge: vendored and versioned (man pages, `--help` dumps, tldr snapshot) — no runtime web lookups
- No API keys, no telemetry, no cloud endpoints in the default config
- Optional web lookup, if ever added: off by default, quarantined, explicit per use

## Non-goals

- Replacing bash as the execution engine
- Fully autonomous operation: the human stays in the loop for anything mutating
- Web access for the command model
- Cloud model backends

## Prior Art and Adopted Ideas

Existing NL shells (Butterfish, nl-sh, nlsh, nuwa-terminal-chat, Warp) all rely on general-purpose, mostly hosted models with confirm prompts as the only safety layer. Ideas taken over:

| Idea | From | How it is used here |
|---|---|---|
| Wrap the real shell, history as model context | Butterfish | PTY-wrapped bash; recent commands and output feed the state block, with taint labels |
| Per-output token cap for history blocks | Butterfish | Truncate each command's output in context; long output goes through the quarantined reader |
| Prefix modes (prompt / agent / one-shot) | Butterfish | Explicit override prefixes (see Input Modes) |
| Local NL-vs-command detection | Warp | Tiny local classifier routes input automatically; trained on own history vs back-translated intents |
| Write executed commands back to shell history | nl-sh | Append final commands to `~/.bash_history`, so plain bash and the NL shell stay in sync |
| Prompt transparency | Butterfish | `:context` shows the exact model input; verbose log mode; prompts in an editable YAML file |
| Fix-on-failure | Butterfish `exec` | Non-zero exit → offer a corrected command (confirm required) |
| Ghost-text autosuggest | Butterfish | Later phase; cheap with a local model (no API cost) |

Deliberately not adopted: any auto-execute / "unsafe" mode (Butterfish `!!`, `@@`, `gencmd -f`), sending credentials to a model server, cloud defaults.

## Input Modes

| Input | Behaviour |
|---|---|
| plain input | Local classifier decides: bash command → run directly (policy gate still applies); natural language → one-shot command proposal |
| `\cmd` | Force raw bash, skip the classifier |
| `?question` | Ask / explain, no command execution (e.g. "why did that fail?") |
| `@request` | Exactly one command, staged for review |
| `@@goal` | Multi-step agent loop (run → observe → next), confirm per step, step limit |
| `:context`, `:history`, `:status` | REPL meta-commands |

Prefixes avoid `!` and `%` because they collide with bash history expansion and job specs.

## Development Hardware (build time only, not part of the deliverable)

| Role | Machine | Notes |
|---|---|---|
| Teacher (data generation, fallback) | Framework Desktop, Ryzen AI Max+ 395, 128 GB unified | llama.cpp / vLLM, OpenAI-compatible endpoint |
| Student training + inference | Desktop, NVIDIA 16 GB | Unsloth QLoRA, 1.5–3B student |
| Reference system | Ubuntu 26.04 VM | Source of truth; execution sandbox with snapshots |

## Deliverable and Target Hardware

What end users get: the REPL + GGUF model weights + install script. No teacher, no training pipeline, no PyTorch at runtime.

**Runtime**
- Inference via llama.cpp (`llama-server` or bindings) with GGUF models — one format across vendors
- Backends: CUDA (NVIDIA), ROCm/HIP (Radeon), Vulkan as universal fallback (Radeon without ROCm, other GPUs), CPU as last resort
- Installer detects the GPU and picks the backend; `:status` shows which one is active

**VRAM budget (16 GB card, leave room for desktop/other apps)**

| Component | Target size |
|---|---|
| Command model (1.5–3B, Q4_K_M–Q8) | 2–4 GB incl. KV cache |
| Quarantined reader | same base model + LoRA adapter (llama.cpp runtime LoRA), or same weights with a different prompt |
| Intent classifier | tiny; runs on CPU |
| **Total target** | **≤ 6 GB VRAM**; must also run CPU-only (slow but functional) |

**Generic vs personal**
- Shipped model is trained on generic 26.04 data only (grounded man-page pairs, tldr, correction pairs, synthetic sessions)
- **The developer's own shell history and session logs never go into the shipped dataset** (paths, hostnames, private infrastructure)
- Personalization happens locally on the user's machine:
  - Retrieval: the user's own `~/.bash_history` as few-shot examples at inference time (no training)
  - Optional: local QLoRA on the user's own session logs (feasible on 16 GB with Unsloth on both vendors) — opt-in, data never leaves the machine
- Reference docs are harvested from the user's own system at install time (installed man pages, `--help`), so they always match installed versions and need not be redistributed

**Target hardware test matrix (examples)**

| Vendor | Cards (16 GB) | Backend |
|---|---|---|
| NVIDIA | RTX 4060 Ti 16 GB, RTX 5060 Ti 16 GB, RTX 5070 Ti, RTX 5080 | CUDA |
| AMD | RX 7800 XT, RX 9060 XT 16 GB, RX 9070 / 9070 XT | ROCm, Vulkan fallback |
| Any | — | CPU-only smoke test |

## Architecture

```
user ──> REPL (Python, prompt_toolkit)
           │  state: cwd, env (filtered), last cmd, exit code,
           │         truncated stdout/stderr, ls of cwd
           ▼
        student model (local) ──> proposed command(s) + risk class
           ▼
        policy gate (deterministic, AST-based)
           read-only    → run directly
           mutating     → show + confirm
           destructive  → explicit confirm (rm -r, dd, mkfs, chmod -R, …)
           privileged   → always confirm (sudo, pkexec, su, doas)
           ▼
        persistent bash in a PTY (cd/export survive)
           ▼
        output → state (with taint labels)
```

- `\` prefix = raw bash passthrough (escape hatch + training data); see Input Modes
- Multi-step tasks: short script output, or a run/observe/next loop with a step limit
- Risk classification: parse with `bashlex` or tree-sitter-bash plus a denylist; the model's own risk label is only a secondary signal
- Never pipe model output directly into the shell

## sudo Handling

- bash runs inside a PTY (`ptyprocess` / `pexpect`), so sudo prompts like in a normal terminal
- On a sudo prompt, the REPL switches to raw passthrough: the keyboard goes straight to the PTY, nothing is logged, state capture is suspended
- Strip the prompt line from captured output
- Credentials are cached per tty → `sudo -v` once per session
- Model may not emit `sudo -S` or pipe anything into sudo
- The privilege check runs on the parsed AST (catches `bash -c "sudo …"`, aliases)
- No blanket `NOPASSWD`; whitelist specific harmless commands via a sudoers drop-in if needed
- **Verify on 26.04:** sudo-rs prompt format, `pwfeedback` (asterisks on by default), `SUDO_PROMPT` support, `timestamp_timeout` behaviour

## Isolating External Information

Untrusted sources include **command output and file contents**, not just the web.

1. **Taint tracking**: every context item is labeled
   - *trusted*: user input, cwd, exit codes, user-typed paths
   - *local-reference*: `man`, `--help`, `apt-cache show`, vendored tldr snapshot
   - *untrusted*: stdout, file contents, anything from the network

   If untrusted content was in context → the proposed command is forced to confirm; sudo is blocked unless typed by the user.
2. **Quarantined reader (dual-LLM pattern)**: a separate model call reads raw untrusted text and may only return schema-constrained output (e.g. `{"flags": [...], "summary": "<=200 chars"}`, enums). The command model never sees the raw text. Reference: Google DeepMind's CaMeL paper.
3. **Process isolation**: the reader/lookup runs in its own container with network and no PTY access; the command model has no network; the REPL is the only bridge and passes only schema-valid data.

Lookup priority: trained knowledge → local man/--help/apt → (last resort) quarantined web.

## Ubuntu 26.04 Specifics to Train In

- sudo-rs is the default `sudo` (no plugin system, pwfeedback on by default)
- uutils (Rust coreutils) provide the core utilities; cp, mv, rm remain GNU in 26.04
- Wayland-only GNOME 50 → `wl-copy`/`wl-paste`, no `xclip`/`xdotool`
- Kernel 7.0; last release with System V init compatibility → prefer systemd units
- Deprecated habits to unlearn: `apt-key`, `ifconfig`/`netstat`, sudo plugin options, X11 tooling
- ROCm available via `apt install rocm`

## Phases

### Phase 0: REPL prototype with teacher backend (1–2 evenings)
- [ ] `prompt_toolkit` REPL + persistent bash in a PTY
- [ ] State capture (cwd, exit code, truncated output, filtered env)
- [ ] AST-based policy gate + confirm UI
- [ ] sudo passthrough mode (no logging)
- [ ] Input modes: `\` raw bash, `?` ask, `@` one-shot, `@@` agent loop, `:` meta-commands
- [ ] Heuristic NL-vs-command router as a placeholder (first token in `$PATH`/builtins → command), replaced later by the trained classifier
- [ ] Teacher backend via local OpenAI-compatible endpoint on the Framework (no API key, LAN only)
- [ ] Prompts in an editable YAML file; `:context` shows the exact model input
- [ ] Write final commands back to `~/.bash_history`
- [ ] Fix-on-failure offer on non-zero exit
- [ ] JSONL session logging (schema below)
- [ ] Run in a throwaway VM first

### Phase 1: 26.04 reference VM (1 evening)
- [ ] Fresh 26.04 VM with the typical package set; clean snapshot
- [ ] Harvest script: man pages, `--help` for everything in `$PATH`, `apt list --installed`, systemd units, `/etc` defaults
- [ ] Verify sudo-rs prompt/env behaviour for the PTY code

### Phase 2: Data pipeline (a weekend)
- [ ] Back-translate bash/zsh history → intent/command pairs
- [ ] Grounded generation: the teacher reads the harvested man page per tool and produces intent→command pairs
- [ ] Execution filter: run each candidate in a fresh VM snapshot; keep passes
- [ ] Correction pairs: "old habit → 26.04 way" from fixable failures
- [ ] Merge accepted/edited commands from the Phase 0 logs
- [ ] Synthetic multi-turn sessions (state usage)
- [ ] Freeze the eval split (incl. 26.04-specific probes) **before** training

### Phase 3: Student training (an evening, then iterate)
- [ ] Pick a 1.5–3B coder base model
- [ ] QLoRA with Unsloth on the 16 GB card
- [ ] Training input format identical to the REPL prompt (state block + intent)

### Phase 3b: Intent classifier
- [ ] Training data: own history (label: command) vs back-translated intents + session prompts (label: natural language)
- [ ] Tiny local model (small encoder or char-level classifier); target < 10 ms per input
- [ ] Replace the Phase 0 heuristic router

### Phase 4: Swap and evaluate
- [ ] REPL backend → student (GGUF via llama.cpp); teacher fallback only as a dev option, not in the shipped config
- [ ] Run the eval suite, compare against the teacher baseline
- [ ] Retrain loop for 26.04.x point releases (re-run Phase 1–2 harvest + filter)

### Phase 5: Package the deliverable
- [ ] Export student (+ reader LoRA) to GGUF; pick quantization per quality/latency on the test matrix
- [ ] Installer: GPU detection → CUDA / ROCm / Vulkan / CPU backend
- [ ] Install-time harvest of local man pages / `--help` for retrieval
- [ ] Packaging: `.deb` or pipx/uv install (avoid strict snap confinement: PTY + sudo need classic access)
- [ ] Run eval suite + latency on each card in the test matrix (NVIDIA and AMD, 16 GB) and CPU-only
- [ ] Opt-in local personalization: history retrieval on by default, local QLoRA as a documented optional step
- [ ] Docs: install, input modes, safety model, what data stays local
- [ ] License check of everything shipped (see Open Questions)

## Data Formats

### Session log (JSONL, one record per turn)
```json
{
  "ts": "2026-10-14T18:30:00+02:00",
  "state": {"cwd": "/home/eduard", "last_cmd": "ls", "exit": 0, "stdout_tail": "…", "taint": ["untrusted:stdout"]},
  "intent": "show disk usage of my home folder sorted",
  "proposed": "du -sh ~/* | sort -h",
  "risk": "read-only",
  "decision": "accepted | edited | rejected",
  "final": "du -sh ~/* 2>/dev/null | sort -h",
  "exit": 0
}
```

### Training pair
```json
{
  "state": {"cwd": "...", "last_cmd": "...", "exit": 0, "stdout_tail": "...", "ls": ["..."]},
  "intent": "...",
  "command": "...",
  "source": "history | grounded | correction | session",
  "verified": true
}
```

## Evaluation Metrics

| Metric | Target (initial) |
|---|---|
| Exact/equivalent command match on held-out history | baseline vs teacher |
| `bash -n` syntax pass rate | ≥ 99% |
| Sandbox execution success | track per category |
| Unrequested destructive proposals | ≈ 0 (blocking metric) |
| 26.04-specific probe accuracy | ≥ teacher |
| Latency (first token / full command) | sub-second on every 16 GB card in the test matrix (NVIDIA and AMD) |
| VRAM footprint | ≤ 6 GB total |
| CPU-only mode | functional, latency documented |

## Open Questions

- Which student base model (size vs latency vs quality)? **Its license must allow redistribution of fine-tuned weights** — licenses can differ between sizes of the same model family
- **Teacher license**: does it permit using its outputs to train a model that is then distributed? Some model licenses restrict this
- Training-data licenses: tldr pages (CC BY 4.0, attribution needed), man-page text (mixed licenses) — affects what goes into the shipped dataset/weights documentation
- Distribution channel: GitHub releases + Hugging Face for weights, PPA, or both?
- When to fall back to the teacher: confidence threshold, task type, or the user's choice?
- How to handle interactive programs (`vim`, `htop`, `less`) inside the PTY: full passthrough?
- Env filtering rules for state capture (secrets, tokens)
- Multi-host support later (NAS, homelab) via SSH, which carries its own trust boundary
