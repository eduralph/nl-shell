---
created: 22.10.2026 16:53
type: adr
status: Proposed
tags:
  - adr
---
# 0026. Issue priority: severity and reach set P0–P3, with safety overrides

## Context

Bug reports reach the repository from several sources: users, the PDCA loop, and code
review findings that were real but deferred out of a PR (for example #174 and #177). Today
an issue gets `bug` or `enhancement` and the `new` label, which the Issue triage workflow
keeps in step with the milestone: an open issue without a milestone carries `new`. Nothing
records how urgent an issue is, so ordering the backlog is done by hand, issue by issue.

Triage needs a priority scheme written down as explicit rules, so that two people, or the
same person on different days, put the same issue at the same priority. This ADR is that
scheme. It applies to all triage, whether done by hand or automated. One planned use is a
model run from a GitHub Action that qualifies new issues, with the maintainer making the
final call; it follows the same rules, so the bot and a human reach the same result.

Two things shape the scheme. First, nl-shell is a shell that runs commands, so a safety
bypass that needs an attacker to set it up is still the main threat (see the threat model
and ADR-0016), even though such a bug is rare in normal use. A plain impact-times-likelihood
table would rank those low. Second, the PDCA loop picks up issues on the open milestone, so
whoever sets a milestone effectively starts work on the issue.

## Decision

We will qualify every issue with three labels, derive a priority from two of them with a
fixed table, and then apply a short list of overrides. Sections 1–3 apply to whoever
triages; section 4 adds what an automated triager may and may not do.

### 1. Labels describing the issue

**Severity: how bad it is when it happens**

| Label | Meaning |
|---|---|
| `sev:critical` | A command runs that the user didn't approve, privilege escalation, data loss, or a secret leaks |
| `sev:major` | A safety check can be bypassed or silently turns off, the session crashes or ends, or a core feature is broken with no workaround |
| `sev:minor` | Wrong behaviour with a workaround, or it degrades in a way the user can see |
| `sev:trivial` | Cosmetic, wording, docs, or a test-only gap |

**Reach: how likely someone hits it**

| Label | Meaning |
|---|---|
| `reach:common` | Normal use triggers it |
| `reach:edge` | Needs an unusual setup, input or timing |
| `reach:adversarial` | Only triggers when someone sets it up on purpose (crafted output, a hostile `.bashrc`, a hostile server) |

**Area: where the bug lives** (for routing and assignment)

`area:session`, `area:terminal`, `area:teacher-client`, `area:targets`, `area:repl`,
`area:docs`, `area:tests`, following the source folders.

### 2. Priority from severity and reach

| | common | edge | adversarial |
|---|---|---|---|
| **critical** | P0 | P1 | P1 |
| **major** | P1 | P2 | P2 |
| **minor** | P2 | P3 | P3 |
| **trivial** | P3 | P3 | P3 |

- **P0:** drop other work, fix now, block the release.
- **P1:** goes on the current milestone.
- **P2:** next milestone, or whenever it fits.
- **P3:** backlog; may be closed if it sits for a long time.

P1 and P2 line up roughly with Codex's review badges, so the meaning is the same whether a
review finding or a bug report produced the issue.

### 3. Overrides

- **Security or safety bugs get at least P1, even when `reach:adversarial`.** In a shell
  that runs commands, a bypass that needs an attacker is still the main threat.
- **`release-gating` means at least P1**, whatever the table says.
- **`accessibility` barriers get at least P2.**
- **Deferred review findings** keep the review's priority, unless the triager can show the
  PR changed the impact.
- **When unsure, leave the priority off.** If the triager can't place severity or reach,
  they set `needs-triage` and say what is missing. Never guess a lower priority.

### 4. Automated triage

When a bot triages, it does this on each new issue:

1. Set type (`bug`, `enhancement`, `documentation`, or `research`), plus severity, reach,
   area, and P0–P3.
2. Post a comment with one line of reasoning for each label, so the maintainer can check
   the call quickly.
3. Assign by area from a fixed owner table. With one maintainer that is the maintainer, or
   the loop for areas the maintainer has approved.
4. Leave the milestone empty. Setting it stays the maintainer's decision. The `new` label
   therefore stays on until the maintainer sets a milestone, as the Issue triage workflow
   already does.

The bot reads issue text, which anyone can write, so it is untrusted input. GitHub's
permissions cannot draw the line we want: the `issues: write` permission that adding a
label needs also allows editing an issue's title and body, closing or locking it, and
setting its milestone. So the model MUST NOT hold a token that can write anything:

- The model runs with read access to the repository only. Its whole output is a verdict
  in a fixed format: the labels, the assignee, and the comment text.
- A separate step that is not a model, and that alone holds the issue-write token, reads
  that verdict and applies it. It accepts only labels from the sets in this ADR (one of
  each kind, plus `needs-triage`), only an assignee from the owner table, and posts the
  comment as text. It never sets a milestone and never changes the title, body or state.
  A verdict that fails these checks is dropped and the issue gets `needs-triage`.

### 5. Recent issues run through it

Classified from titles only, as a check of the scheme rather than real triage:

| Issue | Severity / reach | Priority |
|---|---|---|
| #164: fake terminal reset resumes alt-screen capture | major / adversarial, plus the security override | P1 |
| #172: TIOCSTI checked after bash waits for input | major / adversarial, plus the security override | P1 |
| #170: Ctrl-C during a model request ends the session | major / common | P1 |
| #174: stuck name lookups pile up threads | minor / edge | P3 (P2 if it keeps its review priority) |
| #167: add hidden-character test vectors | trivial | P3 |
| #169: align ADR wording | trivial | P3 |

## Consequences

Every issue carries a priority that says why it has it, and the backlog can be sorted by
label instead of by rereading issues. Manual and automated triage use the same rules, so
either can check the other. The bot's comment makes each automated call cheap to check and
correct.

The security override moves more issues up than any other rule; it is chosen deliberately
and means attacker-only bugs compete with common crashes for the current milestone.
Leaving the milestone to the maintainer keeps the loop from starting work on the bot's
judgement alone, at the cost of one manual step per issue.

Each automated triage run uses model tokens. How much depends mostly on how much code the bot reads,
which the workflow limits.

Follow-on work: create the labels, triage the open issues by these rules, then write the
triage workflow and its prompt from this ADR and test it on existing issues before it runs
on new ones. Once this works on nl-shell, the
severity, reach and priority parts are reused in the other repositories with their own area
labels.

Left open, to settle before this ADR is accepted:

- Whether the scheme covers nl-shell only or every repository. The area labels are
  different in each repository, but the severity, reach and priority parts can be shared.
- Whether the security override stands as written.
- Whether P3 issues ever close automatically, or stay open until the maintainer closes
  them.
- Which rule wins when two disagree: #174 above is P3 by the table but P2 if it keeps its
  review priority.
