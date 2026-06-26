---
name: pr-review
description: Review changes since a fixed point (commit, branch, tag, or merge-base) for adherence to documented coding standards and the originating spec. Use when reviewing a branch, PR, or work-in-progress changes.
---

# PR review

Use this when reviewing a branch, PR, or work-in-progress changes. Review
along two axes: **Standards** (does the code follow this repo's documented
conventions?) and **Spec** (does the code do what the originating
issue/PRD/issue asked for?).

## Gather context

1. **Identify the base.** Get the merge-base or the branch's starting commit:
   `git merge-base <branch> main` or the PR's base ref.
2. **Read the full diff, not just filenames.** `git diff <base>...HEAD` — the
   three-dot form shows changes on the branch only. Skimming filenames hides
   logic changes inside "small" files.
3. **Read the originating spec.** Find the issue, PRD, or request that
   motivated the work. Review against *that*, not against what the code happens
   to do.
4. **Check for documented conventions.** Look for `AGENTS.md`, `CONTRIBUTING.md`,
   `.cursor/rules`, `docs/adr/`, or lint configs that define the repo's
   standards. Review the diff against those, not against personal preference.

## Standards axis

- **Naming** — does the code use the repo's existing naming conventions?
- **Structure** — does new code go where the repo's conventions put it, or did
  it duplicate an existing module?
- **Tests** — is new behaviour covered? Are existing tests still passing?
- **Security** — secrets, injection, unsafe input handling, over-permissive
  tooling.
- **Dead code** — commented-out blocks, unused imports, unreachable branches.
- **Dependencies** — new dependencies justified? Pinned? Available in this
  repo's package manager?

## Spec axis

- Does the diff actually address the originating request, or does it do
  something adjacent?
- Are there spec requirements that are missing from the diff?
- Does the diff introduce behaviour the spec did not ask for (scope creep)?

## Reporting

Prefer **specific, actionable** comments over vague praise. Cite
`file_path:line_number` for every issue. Distinguish between:

- **Blocking** — must fix before merge (bugs, security, spec violations).
- **Suggestion** — worth considering but not blocking (style, alternative
  approach).
- **Question** — genuinely unclear; needs the author's intent.

Do not ask the author to fix things outside the scope of this PR — file those
as separate issues.
