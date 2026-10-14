---
created: 14.10.2026 19:22
type: adr
status: Proposed
tags:
  - adr
---
# 0012. Apache-2.0 license and DCO sign-off

## Context

The project is public and meant to be installed by others. Contributions need a clear
license, and the shipped artifact combines code, model weights and data with their own
licenses.

## Decision

The code is licensed under Apache-2.0. Contributions are accepted under the same license
and gated on the Developer Certificate of Origin (`git commit -s`). Model weights and data
keep their own licenses; the release documents them (M5 license check).

## Consequences

A permissive license with a patent grant, the same as the pdca-harness tooling. The
license check before release must cover weights and data, not only code.
