#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
REPO_DIR="$DATA_DIR/repo"
SETUP_ENV="$DATA_DIR/.setup.env"
OC_CONFIG="$DATA_DIR/opencode.json"
PORT="${PORT:-4096}"

mkdir -p "$DATA_DIR" "$REPO_DIR"

# ---- Load persisted setup (written by the wizard on first run) -----------------
if [ -f "$SETUP_ENV" ]; then
  # set -a exports every variable defined in the file so opencode (a child
  # process) actually sees OPENCODE_SERVER_PASSWORD and the provider key.
  set -a
  # shellcheck disable=SC1090
  . "$SETUP_ENV"
  set +a
fi

# ---- Git identity so the agent can commit/push from the container --------------
git config --global user.name  "${GIT_USER_NAME:-opencode}"  2>/dev/null || true
git config --global user.email "${GIT_USER_EMAIL:-opencode@railway.local}" 2>/dev/null || true
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true

# ---- Ensure a server password (generate + persist if none) ---------------------
if [ -z "$OPENCODE_SERVER_PASSWORD" ]; then
  OPENCODE_SERVER_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
  {
    echo "OPENCODE_SERVER_PASSWORD=$OPENCODE_SERVER_PASSWORD"
  } >> "$SETUP_ENV"
  export OPENCODE_SERVER_PASSWORD
fi

# ---- Detect a usable provider key ---------------------------------------------
provider_key_set() {
  for v in ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY OPENCODE_API_KEY \
           DEEPSEEK_API_KEY GROQ_API_KEY XAI_API_KEY TOGETHER_API_KEY \
           FIREWORKS_API_KEY CEREBRAS_API_KEY MOONSHOT_API_KEY MISTRAL_API_KEY \
           NVIDIA_API_KEY DIGITALOCEAN_ACCESS_TOKEN SNOWFLAKE_CORTEX_TOKEN; do
    eval "val=\${$v:-}"
    if [ -n "$val" ]; then return 0; fi
  done
  return 1
}

# ---- Inject a GitHub PAT into a github.com clone URL --------------------------
inject_token() {
  url="$1"; tok="$2"
  case "$url" in
    https://github.com/*)
      if [ -n "$tok" ]; then
        case "$url" in
          *@*) : ;;            # already has credentials
          *) echo "$url" | sed "s#https://github.com#https://x-access-token:${tok}@github.com#" ; return ;;
        esac
      fi ;;
  esac
  echo "$url"
}

# ---- Decide: guided setup wizard or straight to opencode ----------------------
if ! provider_key_set; then
  echo "[entrypoint] No provider API key found. Starting first-run setup wizard on :$PORT"
  echo "[entrypoint] Open https://<your-railway-domain> in your browser to configure."
  exec python3 /wizard.py --port "$PORT" --data "$DATA_DIR"
fi

# ---- Write opencode runtime config (model only; keys come from env) -----------
write_config() {
  model="${OPENCODE_MODEL:-}"
  small="${OPENCODE_SMALL_MODEL:-}"
  {
    printf '{\n  "$schema": "https://opencode.ai/config.json"'
    if [ -n "$model" ]; then printf ',\n  "model": "%s"' "$model"; fi
    if [ -n "$small" ]; then printf ',\n  "small_model": "%s"' "$small"; fi
    printf ',\n  "share": "disabled",\n  "autoupdate": false\n}\n'
  } > "$OC_CONFIG"
}
write_config

# ---- Clone / update the project repo onto the volume --------------------------
if [ -n "$GIT_REPO" ]; then
  clone_url="$(inject_token "$GIT_REPO" "$GITHUB_TOKEN")"
  if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[entrypoint] Cloning $GIT_REPO ..."
    git clone -- "$clone_url" "$REPO_DIR"
  else
    echo "[entrypoint] Updating $REPO_DIR ..."
    git -C "$REPO_DIR" pull --ff-only --quiet || true
  fi
fi

cd "$REPO_DIR" 2>/dev/null || cd "$DATA_DIR"

echo "[entrypoint] Starting opencode web on 0.0.0.0:$PORT"
echo "[entrypoint] Server URL: https://<your-railway-domain>"
echo "[entrypoint] Auth: user=opencode  password=\$OPENCODE_SERVER_PASSWORD (see Railway vars or $SETUP_ENV)"
exec opencode web --hostname 0.0.0.0 --port "$PORT"
