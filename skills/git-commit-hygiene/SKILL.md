---
name: git-commit-hygiene
description: Write clean conventional commit messages and stage only intended files. Use when preparing a git commit.
---

# Git commit hygiene

Use this when preparing a git commit. Good commits are reviewable, bisectable,
and explain *why* a change was made, not just *what* changed.

## Before committing

1. **Review what will be committed.** Run `git status` and `git diff --staged`
   (or `git diff` before staging). Never commit without looking at the diff.
2. **Stage only intended files.** Use `git add <path>` for specific files, not
   `git add .` or `git add -A`, unless you have verified every changed file is
   intended. Exclude generated files, scratch notes, and secrets.
3. **Never commit secrets.** API keys, tokens, `.env` files, private keys,
   `password.txt` — if you see one in the diff, stop and remove it from the
   index. Consider whether it was already committed in a prior commit.

## Commit message format

Use conventional commit prefixes so history is scannable and changelogs are
generatable:

```
<type>(<optional scope>): <imperative summary>

<optional body explaining why, not what>
```

- **type**: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`,
  `style`, `build`, `ci`
- **summary**: imperative mood ("add" not "added"), lowercase, no trailing
  period, ≤72 characters
- **body**: wrap at 72 characters, explain the motivation and any context a
  reviewer would need. Omit if the summary is self-evident.

## Examples

Good:
```
feat(auth): rate-limit login attempts by IP

Reduces brute-force exposure on the login endpoint. Uses a sliding 60s
window with a 5-attempt threshold before a 15min lockout.
```

Bad (vague, past tense, no type):
```
fixed the bug
```

Bad (mixed concerns):
```
update stuff and refactor and fix login and change colors
```

## Splitting commits

If the diff covers multiple independent changes, split into multiple commits.
A commit should be one logical change. If you find yourself writing "and also"
in the body, it's probably two commits.

## Before pushing

- `git log --oneline -5` to confirm the commits look right.
- If the branch is shared, prefer `--force-with-lease` over `--force` and only
  when the team's workflow permits force-pushing the branch.
- Don't amend or rebase commits that have already been pushed and may be on
  someone else's base.
