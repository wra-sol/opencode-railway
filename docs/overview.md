# Deploy and Host OpenCode Wizard on Railway

The OpenCode Wizard is a first-run web UI that configures a persistent opencode AI coding server on Railway. It validates your LLM provider API key live against the provider's API, populates available models, optionally clones a GitHub repo, and auto-generates a basic-auth password — turning a zero-variable deploy into a working AI pair-programmer in a single page.

## About Hosting OpenCode Wizard

Hosting the OpenCode Wizard means running [opencode](https://opencode.ai) as a long-lived server inside a Railway container. The Dockerfile installs opencode on Debian bookworm-slim; `entrypoint.sh` boots the wizard when no provider key is detected, then switches to `opencode web` once configuration is saved. State — chat sessions, auth tokens, the cloned repo, and `.setup.env` — lives on a Railway persistent volume mounted at `/data`, so everything survives redeploys and laptop shutdowns. You reconnect from a browser (HTTP basic auth, user `opencode`) or `opencode attach` from the terminal. Railway handles public networking, the `$PORT`, and restart policy.

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
- An LLM provider API key from one of: Anthropic, OpenAI, OpenRouter, OpenCode Zen, DeepSeek, Groq, xAI, Together, Fireworks, Cerebras, Moonshot, Mistral, or NVIDIA — or any OpenAI-compatible endpoint via the "custom" provider option.
- A GitHub Personal Access Token (optional — `repo` scope, for cloning private repos and pushing changes back).
- opencode docs: https://opencode.ai

### Implementation Details

The container's behavior is decided at boot by `entrypoint.sh`:

```sh
# No provider key in env → wizard.py on $PORT (first-run setup UI)
# Provider key present     → write opencode.json, clone/pull GIT_REPO, exec opencode web
```

`provider_key_set()` checks for any of 13+ provider env vars. The wizard (`wizard.py`) validates keys live against each provider's `/models` endpoint, populates a searchable model list, validates GitHub PATs against the GitHub API, and persists everything to `/data/.setup.env` (chmod 600) before exiting non-zero so Railway restarts into `opencode web`.

```text
/data
├── .setup.env                # wizard output (shell-sourceable, chmod 600)
├── opencode.json             # model + share/autoupdate settings
├── repo/                     # cloned project the agent works on
└── .local/share/opencode/    # sessions, auth, snapshots (opencode's $HOME)
```

`HOME=/data` is set in the Dockerfile so opencode's session/auth state lives on the persistent volume.

## Why Deploy OpenCode Wizard on Railway?

<!-- Recommended: Keep this section as shown below -->
Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying OpenCode Wizard on Railway, you are one step closer to supporting a complete full-stack application with minimal burden. Host your servers, databases, AI agents, and more on Railway.
<!-- End recommended section -->
