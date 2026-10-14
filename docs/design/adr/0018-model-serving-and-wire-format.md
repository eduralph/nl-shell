---
created: 14.10.2026 20:48
type: adr
status: Proposed
tags:
  - adr
---
# 0018. Model serving and the proposal wire format

## Context

ADR-0008 chose llama.cpp but left open "`llama-server` or bindings", and nothing defined
what the command model returns (#63, #68). Both choices matter:

- **Startup and memory.** Loading 2–4 GB of weights each time the REPL starts makes the
  first answer slow. Two processes (command model and reader) mean two copies of the
  weights and two KV caches, against a ≤ 6 GB budget.
- **A local listener is an input.** An HTTP port on loopback is reachable by every local
  process and, through DNS rebinding, potentially by web pages in the user's browser.
- **Free-form output is fragile.** "Proposed command(s) + risk class" (`docs/plan.md`) has
  no format. A model that answers in prose, or with two commands where one was expected,
  forces the REPL to guess. ADR-0009 fixes the training *input* format to equal the REPL
  prompt; training must match the *output* format too.

Facts checked on the local llama.cpp build (version 10124): `llama-server` binds a Unix
socket when `--host` ends in `.sock`, supports `--api-key`, runtime LoRA adapters
(`--lora`, `--lora-scaled`), parallel slots (`--parallel`), and JSON-schema or grammar
constrained generation per request.

## Decision

1. **One long-lived `llama-server` per user**, run as a systemd user service
   (`nl-shell-model.service`), listening on a **Unix socket** in `$XDG_RUNTIME_DIR` with
   mode `0600`. No TCP port. The REPL connects to it and starts it on demand. The
   command model and the reader are separate requests to the same server, sharing the
   weights (ADR-0017).
2. **Every request uses constrained decoding** against a JSON schema:
   - command model: [`specs/proposal/v1.md`](../specs/proposal/v1.md);
   - reader: [`specs/reader-schema/v1.md`](../specs/reader-schema/v1.md).
3. **Strict parsing, reject not repair.** A response that fails its schema, or exceeds its
   size limits, is discarded. The REPL tells the user that no usable proposal came back;
   it never guesses.
4. **Prompt caching:** the stable part of the prompt (system prompt, few-shot examples)
   comes first and the volatile state block last, so the server's prompt cache can reuse
   it. This is required for the latency targets (#81).
5. **The teacher** (development only, ADR-0003) is reached through the same client with
   the same schemas. A non-loopback URL needs the explicit `--dev-teacher` flag (ADR-0020,
   #77).
6. Training (M3) produces outputs in the proposal format, so the student learns the
   format it is served with.

## Consequences

- The model loads once per login session, not once per REPL start.
- No TCP listener; other local users can't reach the socket. Processes of the same user
  can, which is code running as the user and outside the threat model; the threat model
  lists the socket as an input (#71).
- The REPL never has to interpret prose to find a command.
- A systemd user service is one more install step (#48), and the REPL needs a fallback
  message when the service can't start (no GPU, no weights).
