---
created: 14.10.2026 20:48
type: adr
status: Proposed
supersedes: ADR-0008 (backend order and VRAM budget)
tags:
  - adr
---
# 0019. GPU backend order: CUDA on NVIDIA, Vulkan by default on AMD, ROCm opt-in

## Context

ADR-0008 listed the llama.cpp backends as CUDA (NVIDIA), ROCm/HIP (Radeon), Vulkan as the
universal fallback, and CPU, with the installer choosing. It also stated the ≤ 6 GB VRAM
budget, which is a requirement rather than a decision. The design review (#63, #69) found,
with sources (not independently re-verified):

- On Ubuntu 26.04, `apt install rocm` ships ROCm 7.1.0, behind upstream
  ([Phoronix](https://www.phoronix.com/news/Ubuntu-26.04-With-ROCm)).
- AMD's native-Linux ROCm compatibility matrix lists the RX 7800 XT, 9060 XT, 9070 and 9070
  XT for ROCm 7.2.1, but only for Ubuntu 22.04 and 24.04, not 26.04
  ([AMD](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/native_linux/native_linux_compatibility.html)).
- llama.cpp's RADV Vulkan backend has out-paced its ROCm backend on RDNA4 in Phoronix
  tests ([Phoronix](https://www.phoronix.com/review/llama-cpp-windows-linux/5)).
- Mesa's Vulkan drivers ship with Ubuntu; ROCm is a large extra stack.

## Decision

1. Backend order chosen by the installer (#48):

   | GPU | Default | Option |
   |---|---|---|
   | NVIDIA | CUDA | Vulkan |
   | AMD | **Vulkan** (Mesa RADV) | ROCm, opt-in, if the user has it installed |
   | none / unsupported | CPU | — |

2. The ROCm path never blocks a release (M5): if it fails on 26.04, nl-shell ships with
   Vulkan for AMD.
3. The VRAM budget (≤ 6 GB total, CPU-only must function) is a constraint and lives in
   architecture §2; ADR-0008's statement of it is superseded by that section.
4. The installer measures, rather than assumes: it runs a short benchmark on the chosen
   backend and records it for `:status`.

## Consequences

- AMD users need no ROCm install for the default path.
- The test matrix (#51) tests Vulkan on AMD as the primary path.
- ADR-0008's choice of llama.cpp and GGUF stands; only its backend order and the budget
  statement are superseded.
