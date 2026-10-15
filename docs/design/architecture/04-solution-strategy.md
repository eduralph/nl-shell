---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - strategy
---
# 4. Solution strategy

> Living document.

## 4.1 The shape in one paragraph

A REPL sits between the user and a persistent bash in a PTY. For natural-language input, it
builds a **state block** (cwd, last command, exit code, truncated output, filtered env, each
item labelled with its **taint**) and asks a small local **command model** for a proposal.
A deterministic **policy gate** parses the proposal and classes it; read-only commands run,
everything else waits for a confirm. sudo prompts switch the terminal into **passthrough**,
where nothing is captured. Untrusted text the model needs to understand goes through a
**quarantined reader** that may only return structured data. The command model is a
**student** distilled from a larger **teacher** and trained to know Ubuntu 26.04.

## 4.2 Key strategies

| Problem | Strategy | ADR |
|---|---|---|
| The model can be wrong or steered | Deterministic AST gate; human confirms anything mutating | 0004, 0005 |
| Prompt injection through output | Taint labels; quarantined reader | 0007 |
| The sudo password | PTY passthrough, never captured | 0006 |
| Fast on consumer GPUs | Small distilled student; llama.cpp GGUF on every vendor | 0008, 0009 |
| Knows 26.04 without lookups | Grounded training data filtered by execution in a 26.04 VM | 0009 |
| Privacy | Locally complete; generic training data; local personalization | 0003, 0010 |
| Interaction shape | Own the session in a PTY front end; keep stock bash as the executor | 0002, 0014, 0024 |

## 4.3 Build strategy

Build the REPL and the safety layer first, against the teacher, before any training (M0).
The gate, passthrough and taint tracking are the parts that must be right; they do not depend
on which model proposes. Then build the data, train the student, swap it in and evaluate it,
and package it. See section 9 and proposal 0001.
