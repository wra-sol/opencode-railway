FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git tar unzip openssh-client python3 gnupg gosu \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS — needed for local npx MCP servers
# (memory, sequential-thinking, fetch, brave-search). npx downloads them on
# first use; cache lands on the persistent /data volume so they're kept.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version

# opencode web tries to open a browser via xdg-open; provide a no-op so it
# doesn't log an error in a headless container.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/bin/xdg-open && chmod +x /usr/bin/xdg-open

# Install opencode (as root the installer writes to /root/.opencode/bin). Copy
# the binary to /usr/local/bin so it's reachable when we drop to a non-root user
# (symlinks into /root/ would be inaccessible — /root is 0700 on Debian).
RUN curl -fsSL https://opencode.ai/install | bash \
 && install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode \
 && rm -rf /root/.opencode \
 && opencode --version

# Non-root user for the runtime. HOME=/data (the persistent volume); the
# entrypoint chowns /data at boot then drops to this user via gosu.
RUN useradd --uid 1000 --home-dir /data --shell /bin/sh opencode

# Vendored models.dev snapshot — fallback so the wizard's provider/model
# catalog loads on first deploy even if models.dev is unreachable. The wizard
# refreshes it from the live URL on subsequent runs (cached on the /data volume).
# Non-fatal: if models.dev is down at build time, the wizard fetches live at runtime.
RUN mkdir -p /wizard \
 && (curl -fsSL https://models.dev/api.json -o /wizard/models.dev.snapshot.json \
     || echo "[build] models.dev snapshot fetch failed; wizard will fetch live")

# opencode stores sessions/auth/config under $HOME. Point HOME at the persistent
# Railway volume so state survives redeploys and chat sessions persist.
# OPENCODE_CONFIG points opencode at /data/opencode.json (written by
# generate_config.py) so the model + mcp block is honoured even when the cwd is
# the cloned repo at /data/repo — without this, project config would shadow it.
ENV HOME=/data \
    OPENCODE_DISABLE_AUTOUPDATE=true \
    OPENCODE_CONFIG=/data/opencode.json

WORKDIR /data

COPY entrypoint.sh /entrypoint.sh
COPY prep.sh /prep.sh
COPY wizard.py /wizard.py
COPY generate_config.py /generate_config.py
COPY seed_agents.py /seed_agents.py
COPY skills/ /opt/opencode-skills/
COPY mcps/ /mcps/
RUN chmod +x /entrypoint.sh /prep.sh \
 && python3 -m py_compile /mcps/toolkit.py /wizard.py /generate_config.py /seed_agents.py

EXPOSE 4096

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT:-4096}/global/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
