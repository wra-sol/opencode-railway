#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Seed the always-in-context global AGENTS.md onto the persistent volume.

Writes /data/.config/opencode/AGENTS.md from a bundled template, using a
hash sidecar (.AGENTS.md.bundled.sha256) so the bundled file can be refreshed
on existing deploys without clobbering user edits.

Policy (called by entrypoint.sh on every boot):
  - file absent                        -> seed bundled + write sidecar
  - disk == bundled (already current)  -> no-op (ensure sidecar is correct)
  - disk == sidecar (pristine prior
    bundled version, unedited)         -> upgrade to new bundled + sidecar
  - otherwise (user edited the file,
    or sidecar missing & disk differs) -> preserve, do not overwrite

Stdlib only. Called as: python3 /seed_agents.py --data /data
"""

import argparse
import hashlib
import os
import sys

AGENTS_REL = os.path.join(".config", "opencode", "AGENTS.md")
SIDECAR_REL = os.path.join(".config", "opencode", ".AGENTS.md.bundled.sha256")

BUNDLED_AGENTS_MD = """\
# opencode Railway server

You are running as **opencode** on a headless Railway container, not on a laptop.

## Working directory
The project repo (if configured at setup) is cloned at `/data/repo` and is your
current working directory. When the user says **"the repo"**, they mean
`/data/repo`. If no repo was configured, you start in `/data`.

State — sessions, auth, the cloned repo, and config — lives on the persistent
`/data` volume and survives redeploys.

## Committing & pushing
A GitHub PAT may be injected into the clone URL for private repos and pushes.
**`git push` works with no extra auth** — the PAT is baked into the remote URL
and the container's git identity is configured on boot. Commit and push as
normal; changes are made to the repo **inside the container**, so sync back via
git (push from the container, or the owner pulls from your branch).

Never read or print `/data/.setup.env` — it contains secrets. Don't run
`git remote -v` or echo token env vars: the PAT is embedded in the remote URL
and would leak into the session log.

## Reconnecting
The server keeps running even when no client is connected. Reconnect from a
browser (log in as `opencode` with your password) or from a terminal:
`opencode attach https://<your-railway-domain> -p <password>`.

## Skills & MCPs
Agent skills are available on-demand via the `skill` tool — call it to load a
skill's full instructions when a task matches. For a fuller picture of this
environment (env vars, safe ways to introspect git/PAT state, an orient
procedure), invoke the **`environment-briefing`** skill. MCP servers you
enabled at setup are available as tools alongside the built-in ones; reference
them by name in prompts (e.g. "use context7").
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed(data_dir: str, bundled: str = BUNDLED_AGENTS_MD) -> str:
    """Seed AGENTS.md per the policy above. Returns a status string."""
    agents_path = os.path.join(data_dir, AGENTS_REL)
    sidecar_path = os.path.join(data_dir, SIDECAR_REL)
    os.makedirs(os.path.dirname(agents_path), exist_ok=True)

    bundled_hash = _sha256(bundled)
    disk = None
    disk_hash = None
    if os.path.isfile(agents_path):
        disk = _read_text(agents_path)
        disk_hash = _sha256(disk)

    sidecar_hash = None
    if os.path.isfile(sidecar_path):
        sidecar_hash = _read_text(sidecar_path).strip()

    # Already current — just keep the sidecar honest.
    if disk_hash == bundled_hash:
        if sidecar_hash != bundled_hash:
            _write_text(sidecar_path, bundled_hash + "\n")
        return "current"

    # File absent — first-run seed.
    if disk is None:
        _write_text(agents_path, bundled)
        _write_text(sidecar_path, bundled_hash + "\n")
        return "seeded"

    # Pristine prior bundled version (disk matches the sidecar we last wrote)
    # -> upgrade to the new bundled content.
    if sidecar_hash is not None and disk_hash == sidecar_hash:
        _write_text(agents_path, bundled)
        _write_text(sidecar_path, bundled_hash + "\n")
        return "upgraded"

    # User edited the file (or sidecar is missing and we can't prove it was
    # pristine) -> preserve their edits, do not overwrite.
    return "preserved"


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the global AGENTS.md.")
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    args = ap.parse_args()
    status = seed(args.data)
    print(f"[seed_agents] AGENTS.md: {status}")


if __name__ == "__main__":
    sys.exit(main() or 0)
