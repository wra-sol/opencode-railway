# Template Variables Reference

Reference for configuring the Railway template in the template composer UI.
Railway does **not** read this file — it exists for the template maintainer.

## Guiding principle

**No variable is required to deploy.** If every variable is blank at deploy
time, `entrypoint.sh` boots the first-run setup wizard (`wizard.py`) on the
Railway domain, which collects a login password + provider + key + repo
interactively. Set variables up front only to skip the wizard.

Because Railway's template page lists every variable you add in the composer
under a "variables required" header — and there is no per-variable "optional"
flag — the cleanest UX is to add **zero variables** (or just one pre-filled
secret) so the deploy button isn't intimidating.

## Service settings (template composer)

| Setting | Value |
|---|---|
| Service name | `opencode` |
| Source | GitHub repo: `wra-sol/opencode-railway` |
| Volume mount path | `/data` |
| Public networking | Enabled (Railway assigns `$PORT`) |
| Start command | `/entrypoint.sh` (from `railway.json`) |
| Healthcheck path | `/global/health` (served by the wizard during setup and by `opencode web` after) |

> Railway templates cannot auto-create volumes. The wizard's success page
> reminds users to add the `/data` volume manually if it's missing.

## Recommended composer configuration (minimal)

Add **no variables** in the composer. The deploy flow becomes:

1. User clicks **Deploy** — no form to fill in.
2. Container boots, no server password found → manager serves the first-run wizard on the Railway domain.
3. User sets a login password + picks provider + pastes key (+ optional repo).
4. Settings persist to `/data/.setup.env`; the manager starts `opencode web` as a child (no container restart) and the user is logged in automatically.

### Optional: skip the wizard entirely

The wizard is gated on `OPENCODE_SERVER_PASSWORD` being unset — so to bypass it
and go straight to `opencode web` on first boot, set **both** a password **and**
a provider key in the composer:

| Variable | Value | Why |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | `${{secret(24)}}` | Basic-auth password. Setting this is what skips the wizard. |
| one `<PROVIDER>_API_KEY` | your key | Model access — required if you skip the wizard, or opencode has no model. |

Adding only the password (no provider key) is **not** recommended: the wizard
would be skipped and `opencode web` would come up with no usable model. If
you'd rather use the wizard, add **no variables**.

## Full variable reference (for advanced users / skip-the-wizard deploys)

These can all be set in the Railway service Variables tab after deploy, or
added to the composer if you want to expose them (not recommended — clutters
the deploy page). All are optional.

### Server & auth

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | *(set via wizard)* | Basic-auth password (user is always `opencode`). The wizard requires you to set this on first run. Set it here (with a provider key) to skip the wizard. |
| `PORT` | `4096` | Port the wizard / opencode web listens on. Railway sets this automatically. |
| `DATA_DIR` | `/data` | Persistent volume root. Change only if you mount the volume elsewhere. |

### Model selection

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_MODEL` | opencode default | `provider/model-id`. Optional — leave unset and pick via `/models` in the web UI. Required for custom providers (`custom/<model-id>`). |
| `OPENCODE_SMALL_MODEL` | opencode default | Cheaper model for titles / summaries. Optional — opencode auto-selects when available. |

### Git

| Variable | Default | Description |
|---|---|---|
| `GIT_REPO` | *(none)* | Repo URL opencode clones into `/data/repo` and works on. `https://github.com/you/repo`. |
| `GIT_REPO_BRANCH` | *(none)* | Branch to checkout (default: repo default branch). |
| `GITHUB_TOKEN` | *(none)* | Classic PAT (`repo` scope) injected into the clone URL for private repos + pushes. |
| `GIT_USER_NAME` | `opencode` | Git author name for commits. |
| `GIT_USER_EMAIL` | `opencode@railway.local` | Git author email for commits. |

### MCP servers

| Variable | Default | Description |
|---|---|---|
| `ENABLED_MCPS` | *(none)* | Comma-separated opt-in MCP preset IDs: `context7`, `gh_grep`, `tavily`, `exa`, `memory`, `sequential_thinking`, `fetch`, `brave_search`. |
| `DISABLE_TOOLKIT_MCP` | *(none)* | Set to `1` to turn off the bundled toolkit MCP (a pure-Python local server shipped in the image — on by default, no key needed). |
| `MCP_CUSTOM` | *(none)* | JSON array of custom remote MCPs: `[{"name":"my-server","url":"https://...","headers":{"Authorization":"Bearer ..."} }]`. |
| `TAVILY_API_KEY` | *(none)* | Required if `tavily` in `ENABLED_MCPS`. |
| `EXA_API_KEY` | *(none)* | Required if `exa` in `ENABLED_MCPS`. |
| `BRAVE_API_KEY` | *(none)* | Required if `brave_search` in `ENABLED_MCPS`. |

MCPs missing their required key are silently skipped by `generate_config.py`.
The bundled `toolkit` MCP (in `MCP_PRESETS` with `default_enabled=True`) is on
regardless of `ENABLED_MCPS`; turn it off with `DISABLE_TOOLKIT_MCP=1`. Local npx
MCPs (`memory`, `sequential_thinking`, `fetch`, `brave_search`) need Node.js,
which is included in the image.

### Agent skills

| Variable | Default | Description |
|---|---|---|
| `ENABLED_SKILLS` | *(none)* | Comma-separated skill names: `environment-briefing`, `diagnose`, `git-commit-hygiene`, `pr-review`. |

Selected skills are seeded to `/data/.config/opencode/skills/` on every boot.
Bundled skills are overwritten; user-created skills are preserved.

### Reconfigure

| Variable | Default | Description |
|---|---|---|
| `RECONFIGURE` | *(none)* | Obsolete — reconfigure in-browser at `/setup` on the running server (login required). Accepted for backward compatibility but ignored. |

### LLM provider keys

The wizard collects the user's chosen provider key on submit and writes it to
`/data/.setup.env`. Provider env var names come from [models.dev](https://models.dev).
To skip the wizard, set a password **and** one provider key up front.

Common provider keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`. The full
list is sourced from models.dev at runtime — `wizard.py` (`get_providers`) is the
source of truth.

## Custom / OpenAI-compatible providers

The wizard exposes a "custom" provider option that lets the user paste a base
URL + key + model id. There is no env var for this — it's wizard-only and
persisted to `/data/.setup.env` as custom env vars on submit.

## What NOT to put in the composer

Do **not** add the provider API keys or MCP keys as template variables. They
would all show up on the deploy page as "required"-looking fields, which is
exactly the intimidating UX we're avoiding. The wizard collects the ones the
user actually has.
