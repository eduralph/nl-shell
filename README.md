# {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

<!-- template:begin -->
> **This is a GitHub template repository.** It holds the repo-level governance shared by
> projects driven with the [PDCA quality cycle](https://github.com/eduralph/pdca-harness):
> the agent STOP rules, DCO sign-off, Apache-2.0, the `require-linked-issue` PR check and the
> milestone-driven issue triage. It does **not** hold the PDCA driver — that lives in a
> separate *instance* repo rendered from `pdca-harness` (see [Development process](#development-process)).
>
> ### Start a new project
>
> ```bash
> gh repo create <owner>/<name> --public --template eduralph/pdca-project-template \
>   --description "<one line>" --clone
> cd <name>
> scripts/init-repo.sh          # fills placeholders, labels, first milestone, branch protection
> ```
>
> `init-repo.sh` replaces the `{{…}}` placeholders, commits and pushes that once to `main`,
> creates the `new` label and the first milestone (`Milestone 0.1.0`), then turns on branch
> protection (`require-linked-issue` required, admins included, conversations resolved). It
> removes itself and this block in the same commit. GitHub does not copy labels, milestones or
> protection rules from a template, which is why the script exists.
>
> Then render the PDCA instance next to it:
>
> ```bash
> cd .. && copier copy gh:eduralph/pdca-harness <name>-pdca
> ```
>
> answering `tracker_url` with this repo's issues URL and `contribution_model` = own-repo.
<!-- template:end -->

## Status

Early. See the [open milestone](https://github.com/{{OWNER}}/{{PROJECT_NAME}}/milestones) for
what is in work.

## Development process

Changes to this repo are driven by a PDCA quality cycle (Plan → Do → Check → Act) run from
the companion instance repo
[{{PROJECT_NAME}}-pdca](https://github.com/{{OWNER}}/{{PROJECT_NAME}}-pdca), rendered from
[pdca-harness](https://github.com/eduralph/pdca-harness). The instance keeps the per-issue
result bundles and the process log; this repo keeps only the product.

Every change starts from an issue on the open milestone and ships as one pull request that
closes it. See [CONTRIBUTING.md](CONTRIBUTING.md) and, for coding agents, [AGENTS.md](AGENTS.md).

## Releases

Versions are git tags of the form `vX.Y.0` — a minor bump per release (patch stays `0`);
each tag is annotated with its release notes (`git show vX.Y.0`). The queue for the next
release is a single open **GitHub milestone**:

- There is always **one open milestone — the next version to ship** (the highest released
  tag's minor `+ 1`). It is the live "in work" list.
- **New issues are filed against that open milestone.** An issue with no milestone carries
  the `new` label (set by the Issue triage workflow) until it is triaged.
- **When that version is released** — cut as a `vX.Y.0` tag — its milestone is **closed and a
  new milestone one minor higher is opened**. New issues then go there. Repeat.

Each change ships as its own pull request with a linked issue (one logical change per PR).

## License

Copyright © {{YEAR}} {{AUTHOR}}.

{{PROJECT_NAME}} is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE), [NOTICE](NOTICE)).

Contributions are accepted under Apache-2.0 and gated on the
[Developer Certificate of Origin](DCO) — sign off commits with `git commit -s`. See
[CONTRIBUTING.md](CONTRIBUTING.md).
