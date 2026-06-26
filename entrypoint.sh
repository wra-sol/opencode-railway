#!/bin/sh
# SPDX-License-Identifier: MIT
set -e

DATA_DIR="${DATA_DIR:-/data}"

# As root: fix volume ownership so the non-root user can write to it.
# Idempotent — chown is cheap if ownership is already correct. This also
# handles the migration from earlier root-running versions of this template.
chown -R opencode:opencode "$DATA_DIR" 2>/dev/null || true

# Drop to the non-root opencode user for everything else: prep (config, skills,
# repo clone) and the manager (wizard.py --manage). gosu exec's the target so
# signals (SIGTERM from Railway) propagate cleanly to the manager.
exec gosu opencode sh -c '
  sh /prep.sh
  exec python3 /wizard.py --port "${PORT:-4096}" --data "${DATA_DIR:-/data}" --manage
'
