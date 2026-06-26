# Template Variables Reference

Reference for configuring the Railway template in the template composer UI.
Railway does **not** read this file — it exists for the template maintainer.

## Guiding principle

**No variable is required to deploy.** If every variable is blank at deploy
time, `entrypoint.sh` boots the first-run setup wizard (`wizard.py`) on the
Railway domain, which collects the provider + key + repo interactively. Set
variables up front only to skip the wizard.

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
| Healthcheck path | *(none — wizard returns 200; opencode serves its own)* |

> Railway templates cannot auto-create volumes. The wizard's success page
> reminds users to add the `/data` volume manually if it's missing.

## Recommended composer configuration (minimal)

Add **no variables** in the composer. The deploy flow becomes:

1. User clicks **Deploy** — no form to fill in.
2. Container boots, no provider key found → wizard runs on the Railway domain.
3. User picks provider + pastes key (+ optional repo / password).
4. Settings persist to `/data/.setup.env`, service restarts, `opencode web` comes up.

### Optional: pre-fill the server password

If you want the basic-auth password pre-generated at deploy time instead of on
first boot, add exactly one variable in the composer:

| Variable | Value | Why |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | `${{secret(24)}}` | Railway resolves `${{secret()}}` at deploy time. Appears under "Pre-Configured Environment Variables" and does not block the deploy button. |

`entrypoint.sh` will reuse this instead of generating its own. Either path
works; the auto-generate path in `entrypoint.sh` is the default.

## Full variable reference (for advanced users / skip-the-wizard deploys)

These can all be set in the Railway service Variables tab after deploy, or
added to the composer if you want to expose them (not recommended — clutters
the deploy page). All are optional.

### Server & auth

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | auto-generated | Basic-auth password (user is always `opencode`). If unset, `entrypoint.sh` generates a 24-char string and persists it to `/data/.setup.env`. |
| `PORT` | `4096` | Port the wizard / opencode web listens on. Railway sets this automatically. |
| `DATA_DIR` | `/data` | Persistent volume root. Change only if you mount the volume elsewhere. |

### Model selection

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_MODEL` | opencode default | `provider/model-id`, e.g. `anthropic/claude-sonnet-4-5`. |
| `OPENCODE_SMALL_MODEL` | opencode default | Cheaper model for titles / summaries. |

### Git

| Variable | Default | Description |
|---|---|---|
| `GIT_REPO` | *(none)* | Repo URL opencode clones into `/data/repo` and works on. `https://github.com/you/repo`. |
| `GITHUB_TOKEN` | *(none)* | Classic PAT (`repo` scope) injected into the clone URL for private repos + pushes. |
| `GIT_USER_NAME` | `opencode` | Git author name for commits. |
| `GIT_USER_EMAIL` | `opencode@railway.local` | Git author email for commits. |

### LLM provider keys (set one to skip the wizard)

Set exactly one of these and the wizard is bypassed on next boot. The wizard
writes the chosen one into `/data/.setup.env` on submit.

| Variable | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `OPENROUTER_API_KEY` | OpenRouter |
| `OPENCODE_API_KEY` | OpenCode Zen |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `GROQ_API_KEY` | Groq |
| `XAI_API_KEY` | xAI (Grok) |
| `TOGETHER_API_KEY` | Together |
| `FIREWORKS_API_KEY` | Fireworks |
| `CEREBRAS_API_KEY` | Cerebras |
| `MOONSHOT_API_KEY` | Moonshot (Kimi) |
| `MISTRAL_API_KEY` | Mistral |
| `NVIDIA_API_KEY` | NVIDIA NIM |

`entrypoint.sh:37` (`provider_key_set`) is the source of truth for which keys
trigger the "skip wizard" branch — update that function if you add a new
provider.

## Custom / OpenAI-compatible providers

The wizard exposes a "custom" provider option that lets the user paste a base
URL + key + model id. There is no env var for this — it's wizard-only and
persisted to `/data/.setup.env` as custom env vars on submit.

## What NOT to put in the composer

Do **not** add the 13 provider API keys as template variables. They would all
show up on the deploy page as "required"-looking fields, which is exactly the
intimidating UX we're avoiding. The wizard collects the one the user actually
has.
