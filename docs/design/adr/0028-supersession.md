---
created: 29.10.2026 22:23
type: adr
status: Proposed
tags:
  - adr
  - process
  - documentation
---
# 0028. How a frozen document is superseded

## Context

ADR-0001 and ADR-0027 freeze ratified documents, and both name *superseded* as a later
state. A reader who opens an old document needs to see that it has been replaced, and the
class index needs to say so too. Editing the frozen file in any other way must stay
impossible.

## Decision

Supersession is recorded in three places, in the same pull request:

1. The **new** document carries `supersedes: <old number or path>` in its frontmatter. It is
   a new file, which the immutability check always allows.
2. The **old** document receives a *supersession stamp* where its class has one: an ADR's
   `status:` line changes to `Superseded by ADR-NNNN`, naming the new ADR (several are
   joined by `and`), and a stable spec's `status:` changes to `superseded`. A proposal gets
   no stamp; its index alone records the supersession. Nothing else in the file may change.
   `scripts/docs_checks.py` allows exactly this status edit and nothing more, and a
   superseded document is frozen again.
3. The **class index** (`adr/README.md`, `proposals/README.md`, `specs/README.md`) marks the
   old entry *superseded by NNNN*. Each class MUST keep such an index; it is the place to
   look up a document's current status.

## Consequences

- An old document shows on its own page that it was replaced, and the index agrees.
- The record itself never changes, so references to it stay trustworthy.
- Every ratification or supersession includes an index edit, which is always allowed.
