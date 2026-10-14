---
created: 14.10.2026 21:15
type: adr
status: Proposed
tags:
  - adr
---
# 0022. English input first; replies follow the system locale

## Context

nl-shell takes intent in a human language and turns it into bash (ADR-0002). Nothing so far
says which human languages. The question touches several parts at once:

- The command model is a 1.5–3B student (ADR-0009). Small models lose quality fast outside
  English, and the grounding data is English: man pages, `--help` dumps and tldr
  (ADR-0010).
- The input router decides command vs natural language from the input alone. The M0
  heuristic looks at the first word; the M3 intent classifier is trained on back-translated
  intents. Both are tuned on English, and collisions are language-specific ("find large
  files…" in English, other words in other languages).
- nl-shell also writes to the user. Most of it is fixed text: confirm prompts, errors,
  route replies, and the explanation of a proposal, which the REPL builds from its own
  templates because a proposal carries no free text (spec `proposal/v1`). The model writes
  free text only in ask mode (`?question`).
- Commands run in the user's own bash with the user's locale, so command output, and the
  man pages a user has installed, can be in any language.

Supporting many languages from the start multiplies training data, eval splits and review
work before the English path works at all. Locking the design to English makes adding a
language later a redesign. The safety properties (ADR-0004, ADR-0013) act on the bash
command, not on the request, so they do not depend on the input language.

## Decision

**Input.** English is the only supported input language for the first release. Input in
other languages is not detected or refused; it goes through the same path and works or
fails on a best-effort basis, with no quality promise. We will not add a language detector
as a gate.

**Room to add languages.** We will build the language in from the start so a language can
be added without a redesign:

- Every training and eval example (command model, intent classifier) MUST carry a language
  tag (a BCP 47 code such as `en` or `de`).
- The eval suite MUST report its metrics per language.
- A language becomes supported when it has its own eval split that meets the same
  thresholds as English (plan.md, evaluation), and a UI message catalog. Adding one is a
  later decision that cites this ADR.

**Replies.** nl-shell's own replies follow the system locale (`LC_MESSAGES`, falling back
to `LC_ALL` and `LANG`, per POSIX):

- Fixed UI strings, including the templates that explain a proposal, go through
  gettext-style message catalogs. A missing catalog or string MUST fall back to the
  English text, never to an empty or partial message.
- Free text the model writes (ask mode) uses the locale's language when that language is
  supported, and English otherwise. In the first release this means English.
- The locale only selects among the supported languages and catalogs. Its raw value MUST
  NOT be placed in a model prompt (ADR-0016 treats the `LC_*` variables as untrusted).
- Confirm keys stay `y` / `n` in every locale (for the confirm UI, #74). A confirm is a
  safety step (ADR-0004), and a key that changes with the locale is a way to confirm by
  accident.

**What language never changes.** The proposed command is bash, whatever the input or
locale. The policy gate and the confirm step work on the parsed command and do not look at
the language of the request.

**Locale of the commands themselves.** The user's commands run with the user's locale,
unchanged (ADR-0002). The install-time reference harvest (man pages, `--help`) runs with
`LC_ALL=C.UTF-8`, so the retrieval corpus matches the English the model was trained on.

## Consequences

The first release can be trained, evaluated and tuned in one language, which fits the
student's size and the English grounding data. Non-English speakers get a working shell
with UI text in their language once a catalog exists, but natural-language requests in
their language are unsupported, and the docs must say so.

Two things follow from following the locale while input is English-only: in a non-English
locale the UI text and ask-mode answers can be in different languages until that language
is supported; and the UI needs an i18n layer now, so the M0 route replies, which are
hard-coded English strings in `repl.py`, move into a catalog. Because proposals carry no
free text, most of what a user reads is template text and can be translated without
touching the model.

Command output and error text in the user's locale reach the model and the quarantined
reader (ADR-0017) in that language. Neither is trained for this, so fix-on-failure and
`?question` may be weaker in non-English locales. This is a known gap, not handled here.

The language tags and per-language eval cost little now and are expensive to add to an
existing dataset later. Reversing the reply decision (fixed English) would only mean
dropping the catalogs. Adding an input language means new training data, a new eval split
and possibly a larger student, but no change to the router, gate or confirm design.
