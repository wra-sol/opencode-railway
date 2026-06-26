FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git tar unzip openssh-client python3 \
    && rm -rf /var/lib/apt/lists/*

# opencode web tries to open a browser via xdg-open; provide a no-op so it
# doesn't log an error in a headless container.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/bin/xdg-open && chmod +x /usr/bin/xdg-open

# Install opencode. The installer drops the binary in /root/.opencode/bin; symlink
# it onto the system PATH so it stays reachable when HOME is overridden at runtime.
RUN curl -fsSL https://opencode.ai/install | bash \
 && ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode \
 && opencode --version

# opencode stores sessions/auth/config under $HOME. Point HOME at the persistent
# Railway volume so state survives redeploys and chat sessions persist.
ENV HOME=/data \
    OPENCODE_DISABLE_AUTOUPDATE=true

WORKDIR /data

COPY entrypoint.sh /entrypoint.sh
COPY wizard.py /wizard.py
RUN chmod +x /entrypoint.sh

EXPOSE 4096

ENTRYPOINT ["/entrypoint.sh"]
