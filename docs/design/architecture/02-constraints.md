---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - constraints
---
# 2. Constraints

> Living document. Lists what the design must work within; the decisions that follow from
> these are the ADRs.

## 2.1 Technical constraints

| Constraint | Source |
|---|---|
| Target OS is Ubuntu 26.04 LTS: sudo-rs as `sudo`, uutils core utilities (cp, mv, rm stay GNU), Wayland-only GNOME 50, systemd-only, kernel 7.0 | Plan; section 8.8 |
| Target hardware: one 16 GB NVIDIA or AMD Radeon card; total VRAM use ≤ 6 GB so the desktop keeps working; CPU-only must function | Plan (this section is the normative home of the budget; ADR-0019) |
| Bash is the executor | ADR-0002 |
| No network needed at runtime | ADR-0003 |
| One model runtime for every GPU vendor: llama.cpp with GGUF | ADR-0008 |
| The PTY and sudo need classic (unconfined) access, which rules out a strict snap | ADR-0011 |

## 2.2 Organizational constraints

- One developer, open source, Apache-2.0 with DCO (ADR-0012).
- Changes go through issues, milestones and PRs driven by the PDCA quality cycle in
  [nl-shell-pdca](https://github.com/eduralph/nl-shell-pdca).
- Licenses of the teacher model, the student base model and the training data must allow
  distributing the result. **[OPEN]** — issues in M2 and M3.

## 2.3 Conventions

- Design docs follow `docs/design/README.md`; decisions are ADRs.
- Security-relevant changes update `.oss-scanner/threat_model.md` in the same PR.
