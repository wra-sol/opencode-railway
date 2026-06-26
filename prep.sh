#!/bin/sh
# SPDX-License-Identifier: MIT
# Prep the opencode runtime: load persisted setup, git identity, generate
# opencode.json, seed skills + AGENTS.md, clone/pull the project repo. Called
# by entrypoint.sh on boot AND by the manager (wizard.py) after a /setup save,
# so new settings take effect without a container restart. Idempotent and safe
# to run with no /data/.setup.env (first run): writes a minimal opencode.json
# and skips the repo/skills steps.
set -e

DATA_DIR="${DATA_DIR:-/data}"
REPO_DIR="$DATA_DIR/repo"
SETUP_ENV="$DATA_DIR/.setup.env"
SEED_SKILLS_DIR="/opt/opencode-skills"

mkdir -p "$DATA_DIR" "$REPO_DIR"

# Load persisted setup (written by the wizard). set -a exports every var so
# child processes (generate_config, opencode) see OPENCODE_SERVER_PASSWORD + keys.
if [ -f "$SETUP_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SETUP_ENV"
  set +a
fi

# Git identity so the agent can commit/push from the container.
git config --global user.name  "${GIT_USER_NAME:-opencode}"  2>/dev/null || true
git config --global user.email "${GIT_USER_EMAIL:-opencode@railway.local}" 2>/dev/null || true
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true

# Write opencode runtime config (model + mcp block; keys via {env:VAR}).
python3 /generate_config.py

# Seed opt-in skills onto the persistent volume. Bundled skill dirs are always
# overwritten so selection stays accurate and image updates apply; user-created
# skills (different names) are never touched.
seed_skills() {
  skills_dir="$DATA_DIR/.config/opencode/skills"
  mkdir -p "$skills_dir"
  [ -d "$SEED_SKILLS_DIR" ] || return 0
  for bundled in "$SEED_SKILLS_DIR"/*/; do
    [ -d "$bundled" ] || continue
    name=$(basename "$bundled")
    rm -rf "${skills_dir:?}/$name"
  done
  if [ -n "$ENABLED_SKILLS" ]; then
    OLDIFS="$IFS"
    IFS=','
    for s in $ENABLED_SKILLS; do
      s=$(echo "$s" | tr -d ' ')
      [ -n "$s" ] || continue
      if [ -d "$SEED_SKILLS_DIR/$s" ]; then
        cp -r "$SEED_SKILLS_DIR/$s" "$skills_dir/$s"
      fi
    done
    IFS="$OLDIFS"
  fi
}
seed_skills

# Seed a global AGENTS.md (versioned re-seed; user edits preserved).
python3 /seed_agents.py --data "$DATA_DIR"

# Inject a GitHub PAT into a github.com clone URL + redact it from logs.
inject_token() {
  url="$1"; tok="$2"
  case "$url" in
    https://github.com/*)
      if [ -n "$tok" ]; then
        case "$url" in
          *@*) : ;;
          *) echo "$url" | sed "s#https://github.com#https://x-access-token:${tok}@github.com#" ; return ;;
        esac
      fi ;;
  esac
  echo "$url"
}
redact_token() { sed "s#x-access-token:[^@]*@#x-access-token:***@#g"; }

# Clone / update the project repo onto the volume.
if [ -n "$GIT_REPO" ]; then
  clone_url="$(inject_token "$GIT_REPO" "$GITHUB_TOKEN")"
  if [ ! -d "$REPO_DIR/.git" ]; then
    branch_arg=""
    [ -n "$GIT_REPO_BRANCH" ] && branch_arg="--branch $GIT_REPO_BRANCH"
    echo "[prep] Cloning $GIT_REPO ..."
    # shellcheck disable=SC2086 # branch_arg intentionally word-split
    if ! git clone $branch_arg -- "$clone_url" "$REPO_DIR" 2>/tmp/clone.err; then
      redact_token < /tmp/clone.err >&2
      exit 1
    fi
  else
    echo "[prep] Updating $REPO_DIR ..."
    git -C "$REPO_DIR" pull --ff-only --quiet 2>&1 | redact_token || true
  fi
fi
