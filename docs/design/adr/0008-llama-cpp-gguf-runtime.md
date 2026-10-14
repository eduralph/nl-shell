---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0008. llama.cpp with GGUF models as the one runtime across GPU vendors

## Context

The deliverable must run on any 16 GB NVIDIA or AMD Radeon card, and without a GPU. Vendor
stacks differ: CUDA, ROCm, Vulkan. Shipping PyTorch at runtime is large and vendor-specific.

## Decision

We will run all models through llama.cpp (`llama-server` or bindings) with GGUF weights.
Backends: CUDA for NVIDIA, ROCm/HIP for Radeon, Vulkan as the universal fallback, CPU as the
last resort. The installer detects the GPU and picks the backend; `:status` shows which one
is active. There is no PyTorch at runtime.

## Consequences

One model format for every vendor, and runtime LoRA for the quarantined reader. The VRAM
target is ≤ 6 GB in total. Training (ADR-0009) still uses PyTorch, so models are exported to
GGUF and quantized per card (M5).
