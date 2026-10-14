---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - deployment
---
# 7. Deployment view

> Living document.

## 7.1 The user's machine (the deliverable)

- The REPL + GGUF weights + an install script. No teacher, no training pipeline, no PyTorch.
- One `llama-server` per user, run as the systemd user service `nl-shell-model.service` on
  a Unix socket, serving both the command model and the reader (ADR-0018).
- llama.cpp backend picked at install time (ADR-0019): CUDA on NVIDIA; Vulkan (Mesa RADV)
  by default on AMD, ROCm opt-in; CPU as the last resort.
- Local reference index harvested at install time from dpkg-owned man pages and an
  allowlist of `--help` outputs, without running arbitrary binaries (#23, #49).
- Files under the XDG base directories (ADR-0020): config and prompts in
  `~/.config/nl-shell/`, weights and the reference index in `~/.local/share/nl-shell/`,
  session logs in `~/.local/state/nl-shell/` (private to the user).

VRAM budget on a 16 GB card:

| Component | Target |
|---|---|
| Command model (1.5–3B, Q4_K_M–Q8) | 2–4 GB incl. KV cache |
| Quarantined reader | Same base weights + a runtime LoRA adapter, or the same weights with another prompt |
| Intent classifier | Tiny; runs on CPU |
| **Total** | **≤ 6 GB**; also runs CPU-only (slow but functional) |

## 7.2 Test matrix

| Vendor | Cards (16 GB) | Backend |
|---|---|---|
| NVIDIA | RTX 4060 Ti 16 GB, RTX 5060 Ti 16 GB, RTX 5070 Ti, RTX 5080 | CUDA |
| AMD | RX 7800 XT, RX 9060 XT 16 GB, RX 9070 / 9070 XT | ROCm, Vulkan fallback |
| Any | — | CPU-only smoke test |

## 7.3 Development hardware (build time only)

| Role | Machine | Notes |
|---|---|---|
| Teacher (data generation, dev fallback) | Framework Desktop, Ryzen AI Max+ 395, 128 GB unified | llama.cpp / vLLM, OpenAI-compatible endpoint on the LAN |
| Student training + inference | Desktop, NVIDIA 16 GB | Unsloth QLoRA |
| Reference system | Ubuntu 26.04 VM | Source of truth; execution sandbox with snapshots |

**[OPEN]** Distribution channel for weights and package: GitHub releases, Hugging Face, a
PPA, or a mix (M5.4).
