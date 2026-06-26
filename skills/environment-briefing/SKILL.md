---
name: environment-briefing
description: Context for running as opencode on a Railway container — /data layout, env vars, commit & push with the injected GitHub PAT (and safe ways to introspect it), reconnect via opencode attach, available MCPs and seeded skills.
---

# opencode Railway server — environment briefing

You are running as **opencode** on a headless Railway container, not on a local
laptop. There is no browser, no desktop, and no interactive TUI on the host.
Users connect to you remotely via a web browser or `opencode attach`.

## Working directory

The project repo (if configured at setup) is cloned at `/data/repo` and is your
current working directory. When the user says **"the repo"**, they mean
`/data/repo`. If no repo was configured, you start in `/data`.

State lives on the persistent `/data` volume and survives redeploys:

```
/data
├── .setup.env              # wizard output (secrets — do not cat into commits)
├── opencode.json           # model + MCP server config
├── repo/                   # the cloned project you work on
├── .config/opencode/
│   ├── skills/             # seeded agent skills (opt-in)
│   ├── AGENTS.md           # always-in-context environment rules
│   └── .AGENTS.md.bundled.sha256   # hash sidecar for AGENTS.md re-seed
└── .local/share/opencode/  # sessions, auth, snapshots
```

## Environment variables (source of truth)

The container is configured by these env vars (set by the wizard into
`/data/.setup.env` or via Railway variables):

| Var | Purpose |
|---|---|
| `GIT_REPO` | Repo URL cloned into `/data/repo`. |
| `GIT_REPO_BRANCH` | Branch checked out (default: repo default). |
| `GITHUB_TOKEN` | Classic PAT (`repo` scope) for private repos + pushes. |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | Git identity for commits. |
| `HOME` | `/data` — opencode stores sessions/auth/config here. |
| `OPENCODE_CONFIG` | `/data/opencode.json` — model + MCP block. |

## Committing & pushing

A GitHub PAT may have been injected into the clone URL (as
`x-access-token:<token>@github.com`) by `entrypoint.sh` so private repos clone
and the agent can push. Git identity (`user.name`, `user.email`) is configured
globally on boot. Changes are made to the repo **inside the container**, so
sync back via git (push from the container, or the owner pulls from your
branch).

**`git push` works with no extra auth** — the PAT is baked into the remote URL.

### Safe introspection (do NOT leak the token)

The PAT is embedded in the remote URL, so any command that prints it leaks the
token into the session log. Avoid:

- `git remote -v` / `git remote show origin` — prints `x-access-token:<TOKEN>@`.
- `echo $GITHUB_TOKEN` / `printenv GITHUB_TOKEN` — prints the raw token.
- `cat /data/.setup.env` — contains this and other secrets.

To check whether a PAT is configured **without** leaking it:

```sh
[ -n "$GITHUB_TOKEN" ] && echo "PAT configured" || echo "no PAT"
```

To confirm push works without leaking anything:

```sh
git push -n      # dry-run; resolves the remote but prints no URL
```

## Reconnecting

The server keeps running even when no client is connected. The owner reconnects
from a browser (log in as `opencode` with their password) or from a terminal:

```
opencode attach https://<your-railway-domain> -p <password>
```

## Skills & MCPs

Agent skills are available on-demand via the native `skill` tool. Call it to
load a skill's full instructions when a task matches its description. Seeded
skills (if enabled at setup) live in `/data/.config/opencode/skills/`.

MCP servers enabled at setup are available as tools alongside the built-in
tools. Reference them by name in prompts, e.g. "use context7" or "use the
gh_grep tool". MCP tools add to context cost, so prefer built-in tools when
they suffice.

## What to do first

If this is the start of a session and the owner hasn't given a specific task,
orient yourself on the project's current state:

```sh
pwd                                  # should be /data/repo (or /data)
git rev-parse --show-toplevel        # confirm the repo root
git status
git log --oneline -5
```
