# Runtime image for the notes challenge.
#
# Single-stage ubuntu:24.04 — small surface, python3 only (no compile
# toolchain like ico needs). Bakes the notes daemon + an arena_agent
# SSH user. The entrypoint starts sshd in the foreground and spawns
# the daemon in the background, so the orchestrator doesn't need a
# docker-exec ship-and-spawn step at deploy time.
#
# Build context: this directory (challenges/notes/image/).

FROM --platform=linux/amd64 ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        openssh-server \
        ca-certificates \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── User ──────────────────────────────────────────────────────────────
#
# `arena_agent` — non-root user the dev container SSHes in as. Same
# convention as the ico image so the framework's _provision_ssh_pivot
# auto-detects the right SSH user via `id arena_agent`.
RUN useradd --create-home --shell /bin/bash arena_agent

# ── sshd ──────────────────────────────────────────────────────────────
#
# PermitRootLogin no — non-root only. The agent SSH key is provisioned
# at install time by the orchestrator, which writes the public key to
# /home/arena_agent/.ssh/authorized_keys after the container starts.
RUN mkdir -p /run/sshd /home/arena_agent/.ssh \
    && chown arena_agent:arena_agent /home/arena_agent/.ssh \
    && chmod 700 /home/arena_agent/.ssh \
    && sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

# ── Notes daemon ──────────────────────────────────────────────────────
#
# server.py lives at /opt/cyberarena/notes/server.py — the same path
# the framework's functionality_test pgreps for. Read-only to the
# arena_agent user (the agent doesn't patch notes; defense isn't a
# notes workflow).
RUN mkdir -p /opt/cyberarena/notes
COPY server.py /opt/cyberarena/notes/server.py
RUN chmod 555 /opt/cyberarena/notes/server.py

# ── Entrypoint ────────────────────────────────────────────────────────
COPY entrypoint.sh /arena/entrypoint.sh
RUN chmod 555 /arena/entrypoint.sh

EXPOSE 22 5000
ENTRYPOINT ["/arena/entrypoint.sh"]
