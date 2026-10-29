---
created: 29.10.2026 22:23
type: adr
status: Proposed
tags:
  - adr
  - process
  - proposals
  - specs
---
# 0027. Proposal and specification process, lifecycle, and immutability

## Context

ADR-0001 defines the process for ADRs only. Proposals and specifications need their own,
because they carry review history, implementation scope, compatibility contracts or
conformance requirements, and an accepted plan or a stable contract that can still be
edited in place is not a reliable record.

The classes still differ: the architecture overview is living and changes with the system;
ADRs are historical decisions; proposals are accepted implementation plans; specifications
are normative compatibility contracts.

## Decision

Proposals and specifications use YAML frontmatter as their lifecycle source of truth, as
ADRs do. Their accepted or stable states are frozen and change by supersession, replacement
or a new version, never by rewriting the file.

### Proposals

Proposals live in `docs/design/proposals/`, named `NNNN-kebab-title.md` with a number that is
never reused. Drafts live in `draft/`; accepted, implemented, superseded or withdrawn
proposals live in `accepted/`. Valid `status:` values:

- **draft**: a working proposal; edit freely.
- **accepted**: ratified as the plan of record. The file moves from `draft/` to `accepted/`
  and `status:` becomes `accepted` in the same change.
- **implemented**: the scoped work has landed. Terminal.
- **superseded**: a later proposal replaces it. The proposals index records this;
  the file is not stamped (ADR-0028).
- **withdrawn**: explicitly abandoned. Terminal.

After a proposal leaves `draft`, its body is not rewritten. If the plan changes materially,
write a new proposal that supersedes or rescopes it. Small implementation facts go in the
living architecture or in code comments.

### Specifications

Specifications live in `docs/design/specs/<domain>/v<N>.md` and use
`../templates/spec.md`. Valid `status:` values:

- **draft**: under development; edit freely; must carry an instability marker.
- **stable**: a ratified contract for its `version:`. `status:` and `stability:` both
  become `stable` in the same change.
- **superseded**: a later version or spec replaces it (ADR-0028).
- **withdrawn**: abandoned before it became stable.

Any normative change to a stable spec (fields, wire behavior, bytes on disk, compatibility
guarantees, required errors, reader or writer behavior) needs a new version or a
superseding spec. Conformance vectors under a version directory are part of that spec:
changing, deleting or adding one for a stable version is a spec change.

### CI enforcement

`scripts/docs_checks.py immutability`, run by the required `docs` check, covers ADRs,
proposals and specs. It compares the pull request with its merge base, and counts a record as
frozen if it is frozen at the merge base or at the tip of the base branch. Draft and Proposed
records may be edited or ratified, and new records and index/README edits are allowed. A
frozen record may change only its `status:` line, and only to a listed next state: an
Accepted ADR to `Superseded by ADR-NNNN` (ADR-0028), an accepted proposal to `implemented`
or `withdrawn`, and a stable spec to `superseded` or `withdrawn`.

## Consequences

- Accepted proposals and stable specs are records people can rely on.
- Changes after acceptance need a new document, a small and intended cost.
- One CI check covers all three ratified classes.
