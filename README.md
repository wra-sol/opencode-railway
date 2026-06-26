# opencode on Railway

Deploy [opencode](https://opencode.ai) as a persistent server on Railway so your
AI coding sessions keep running even when your laptop shuts down. Reconnect from
your browser or terminal at any time.

- **First-run setup wizard** — open the Railway domain, pick your LLM provider,
  paste a key, point it at a repo, and go. No CLI required.
- **Persistent sessions** — state lives on a Railway volume at `/data`, so chats,
  auth, and the cloned project survive redeploys.
- **Any provider** — Anthropic, OpenAI, OpenRouter, OpenCode Zen, DeepSeek, Groq,
  xAI, and more, plus a custom option for anything OpenAI-compatible.
- **Secured by default** — HTTP basic auth on the public URL; password is
  auto-generated if you don't set one.

> ### 💡 Just click Deploy — no variables required
> Every Railway template variable is **optional**. If you leave them all blank,
> the first-run **setup wizard** launches on your Railway domain and walks you
> through picking a provider, pasting a key, and (optionally) pointing at a repo.
> Set variables up front only if you want to skip the wizard.

---

## Deploy

### Option A — Deploy from this repo

1. Fork or push this repo to your GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → select it. Railway
   auto-detects the `Dockerfile` and `railway.json`.
3. **Add a persistent volume**: service **Settings → Volumes → Add Volume**,
   mount path **`/data`** (this is what makes sessions survive redeploys).
4. (Optional) Set variables up front to skip the wizard — see below.
5. Deploy. Open the generated `*.up.railway.app` domain:
   - If you didn't set variables, the **setup wizard** appears. Fill it in once.
   - If you did, log in as `opencode` with your `OPENCODE_SERVER_PASSWORD`.

### Option B — Publish as a one-click Railway template

Templates must come from a **public** repo. Once this repo is public:

1. Go to <https://railway.com/button>.
2. Point it at your repo. **You do not need to add any template variables** —
   leave the variables list empty (or add only `OPENCODE_SERVER_PASSWORD` with
   value `${{secret(24)}}` if you want to pre-fill it). When the user deploys
   with nothing set, the setup wizard handles the rest. See
   [`TEMPLATE_VARIABLES.md`](./TEMPLATE_VARIABLES.md) for the full maintainer
   reference.
3. Save → you get a **Deploy on Railway** button URL you can share.

> The volume at `/data` still needs to be added after deploy (Railway templates
> don't auto-create volumes). The setup wizard's success page reminds users of this.

---

## First-run setup wizard

When the container boots with no provider API key in the environment,
`wizard.py` serves a one-page web UI on the Railway domain. Three sections,
all optional except the provider + key:

| Step | Field | Required | Notes |
|---|---|---|---|
| 1 · Provider | LLM provider | yes | Anthropic, OpenAI, OpenRouter, OpenCode Zen, DeepSeek, Groq, xAI, Together, Fireworks, Cerebras, Moonshot, Mistral, NVIDIA, or custom OpenAI-compatible. |
| 1 · Provider | API key | yes | Stored to `/data/.setup.env` (chmod 600). |
| 1 · Provider | Model id | no | Blank = opencode default for the chosen provider. |
| 2 · Repo | Repo URL | no | Cloned into `/data/repo`. Set later if you like. |
| 2 · Repo | GitHub PAT | no | Classic PAT with `repo` scope for private repos / pushing. |
| 3 · Server | Password | no | Blank = auto-generated. |
| 3 · Server | Git author name | no | Defaults to `opencode`. |

![Setup wizard — provider step](docs/wizard-provider.png)
![Setup wizard — repo step](docs/wizard-repo.png)
![Setup wizard — success page](docs/wizard-success.png)

> Screenshots are placeholders — drop real PNGs into `docs/` with these names.

On submit, settings persist to `/data/.setup.env`, the service restarts, and
`opencode web` comes up behind basic auth (`opencode` / your password).

---

## Configuration

All config is optional — leave it blank and use the wizard, or set Railway
variables to go straight to opencode.

| Variable | Purpose |
|---|---|
| `OPENCODE_SERVER_PASSWORD` | Basic-auth password (user defaults to `opencode`). Auto-generated if unset. |
| `OPENCODE_MODEL` | `provider/model-id`, e.g. `anthropic/claude-sonnet-4-5`. |
| `OPENCODE_SMALL_MODEL` | Cheaper model for titles/summaries. |
| `GIT_REPO` | Repo the agent clones into `/data/repo` and works on. |
| `GITHUB_TOKEN` | Classic PAT injected into the clone URL for private repos + pushes. |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | Git identity for commits (default `opencode`). |
| `<PROVIDER>_API_KEY` | Any of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `CEREBRAS_API_KEY`, `MOONSHOT_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY`. |

> **Note:** opencode works on the copy of your repo **inside the container**
> (at `/data/repo`), not the files on your laptop. Sync changes back via git —
> the agent can commit & push, or you pull from its branch.

---

## Reconnect from your laptop

```bash
# Browser
open https://<your-app>.up.railway.app   # log in: opencode / <password>

# Terminal (TUI over the remote server)
opencode attach https://<your-app>.up.railway.app -p <password>
```

Sessions live on the `/data` volume, so they survive redeploys and your laptop
shutting down.

---

## How it works

```
Railway deploy
   └─ entrypoint.sh
        ├─ source /data/.setup.env          # persisted wizard output
        ├─ ensure OPENCODE_SERVER_PASSWORD   # auto-generate + persist if missing
        ├─ provider key present?
        │     no  → wizard.py on $PORT       # first-run setup UI
        │     yes → write opencode.json, clone/pull GIT_REPO, exec opencode web
        └─ opencode web --hostname 0.0.0.0 --port $PORT
```

`/data` layout:
```
/data
├── .setup.env          # wizard output (shell-sourceable, chmod 600, secrets here)
├── opencode.json       # model + share/autoupdate settings
├── repo/               # cloned project the agent works on
└── .local/share/opencode/   # sessions, auth, snapshots (opencode's $HOME)
```

---

## License

MIT
