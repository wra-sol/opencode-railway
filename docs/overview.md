# Deploy and Host OpenCode Wizard on Railway

The OpenCode Wizard is a first-run web UI that configures a persistent opencode AI coding server on Railway. It has you set a login password, validates your LLM provider API key live against the provider's API, and optionally clones a GitHub repo — turning a zero-variable deploy into a working AI pair-programmer in a single page.

## About Hosting OpenCode Wizard

Hosting the OpenCode Wizard means running [opencode](https://opencode.ai) as a long-lived server inside a Railway container. The Dockerfile installs opencode on Debian bookworm-slim; `entrypoint.sh` boots the wizard when no server password is set, then switches to `opencode web` once configuration is saved. State — chat sessions, auth tokens, the cloned repo, and `.setup.env` — lives on a Railway persistent volume mounted at `/data`, so everything survives redeploys and laptop shutdowns. You reconnect from a browser (HTTP basic auth, user `opencode`) or `opencode attach` from the terminal. Railway handles public networking, the `$PORT`, and restart policy.

## Common Use Cases

- **Persistent AI coding sessions** — kick off a long-running refactor or debugging session, close your laptop, and reconnect from any browser or terminal. The session keeps running on the server.
- **Remote pair-programmer for a team** — share a single opencode endpoint behind basic auth so multiple people can hop into the same AI coding session from anywhere.
- **Unattended work on a fixed repo** — point the wizard at a GitHub repo, provide a PAT, and the agent clones it into `/data/repo` and can commit & push on your behalf while you're away.

## Dependencies for OpenCode Wizard Hosting

- **opencode** — installed via the official `curl` installer in the Dockerfile; the AI coding server binary itself.
- **Python 3 (stdlib only)** — the wizard (`wizard.py`) ships in the image and uses only the standard library; no pip packages required.
- **Git** — for cloning the project repo into `/data/repo` and for the agent to commit and push changes.
- **A Railway persistent volume at `/data`** — required for sessions, auth, and the cloned repo to survive redeploys. Railway templates cannot auto-create volumes, so the wizard's success page reminds users to add one.

### Deployment Dependencies

- opencode installer: https://opencode.ai/install
- An LLM provider API key from any provider listed on [models.dev](https://models.dev) — Anthropic, OpenAI, OpenRouter, OpenCode Zen, DeepSeek, Groq, xAI, and many more — or any OpenAI-compatible endpoint via the "custom" provider option. The full catalog is sourced from models.dev at runtime.
- A GitHub Personal Access Token (optional — `repo` scope, for cloning private repos and pushing changes back).
- opencode docs: https://opencode.ai

### Implementation Details

At boot, `entrypoint.sh` runs `prep.sh` (load `/data/.setup.env`, git identity,
`generate_config.py`, seed skills + a global `AGENTS.md`, clone/pull `GIT_REPO`)
then execs `wizard.py --manage` — a persistent manager that owns `$PORT`:

```sh
# entrypoint.sh → prep.sh (config/skills/repo) → exec wizard.py --manage
#   manager: not configured? serve /setup (first-run form, no auth)
#            configured?     spawn `opencode web` on 127.0.0.1:(PORT+1),
#                            serve /manage (dashboard/logs/restart), and
#                            reverse-proxy everything else to the child
#                            (injecting opencode's basic auth behind a session)
```

First-run setup is gated on `OPENCODE_SERVER_PASSWORD` being unset — it's the one
thing the user must set so they know how to log in. The manager collects the
provider key (validated live against each provider's `/models` endpoint),
validates GitHub PATs against the GitHub API, and persists everything to
`/data/.setup.env` (chmod 600). On save it re-runs `prep.sh` and (re)starts the
`opencode web` child without a container restart. Reconfiguring later is an
in-browser action at `/setup` (login required) — no env vars or restarts.

```text
/data
├── .setup.env                     # wizard output (shell-sourceable, chmod 600, secrets here)
├── opencode.json                  # model + mcp block (written by generate_config.py)
├── repo/                          # cloned project the agent works on
├── .config/opencode/
│   ├── skills/                    # seeded agent skills (opt-in)
│   ├── AGENTS.md                  # always-in-context environment briefing
│   └── .AGENTS.md.bundled.sha256  # hash sidecar — lets bundled AGENTS.md updates reach existing deploys safely
└── .local/share/opencode/         # sessions, auth, snapshots (opencode's $HOME)
```

`HOME=/data` is set in the Dockerfile so opencode's session/auth state lives on the persistent volume.

## Why Deploy OpenCode Wizard on Railway?

<!-- Recommended: Keep this section as shown below -->
Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying OpenCode Wizard on Railway, you are one step closer to supporting a complete full-stack application with minimal burden. Host your servers, databases, AI agents, and more on Railway.
<!-- End recommended section -->
