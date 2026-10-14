---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - goals
---
# 1. Introduction and goals

> Living document. Describes the design; the project is pre-code (no milestone delivered
> yet). Open questions are marked **[OPEN]** inline.

## 1.1 What this is

nl-shell is a natural-language REPL for Ubuntu 26.04. You type what you want; a small model
running on your own GPU proposes a bash command; a deterministic policy gate decides whether
it runs directly or waits for your confirmation; a persistent bash session runs it.

It replaces bash **as the interface**, not as the executor (ADR-0002). Everything it runs is
an ordinary bash command you can read, edit and rerun without nl-shell.

## 1.2 The headline promise

**You stay in control of what runs, and your sudo password stays yours.** Nothing that
changes the system runs without your confirm (ADR-0004), the command you confirm is exactly
the command that runs, and the password never reaches the model, a log or the history
(ADR-0006). The threat model rates breaking either as Critical.

## 1.3 Quality goals

| # | Goal | Meaning |
|---|------|---------|
| 1 | **Safety** | No unconfirmed mutating or privileged command; no secret leaks; untrusted text cannot steer a command past the gate |
| 2 | **Locally complete** | Works with the network unplugged; no cloud, keys or telemetry (ADR-0003) |
| 3 | **Fast** | Sub-second proposals on any 16 GB NVIDIA or AMD card; functional on CPU |
| 4 | **Knows 26.04** | Proposes the 26.04 way (sudo-rs, uutils, Wayland, systemd) without lookups |
| 5 | **Installable** | Anyone with a supported GPU installs it without training anything |

When goals conflict, the lower number wins: a slower confirm beats an unsafe shortcut.

## 1.4 Stakeholders and target users

- **Users:** Ubuntu 26.04 desktop users who know roughly what they want but not the exact
  command, and experienced users who want to type less. They have a 16 GB NVIDIA or AMD
  card, or accept slow CPU-only mode.
- **Developer:** builds the teacher/student pipeline on their own hardware (section 7).

## 1.5 Scope boundaries

In scope: the REPL, the policy gate, the PTY session, local model inference, the data and
training pipeline (build time), the installer.

Out of scope (non-goals from `docs/plan.md`): replacing bash as the executor; fully
autonomous operation; web access for the command model; cloud model backends.
