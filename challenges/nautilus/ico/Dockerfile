# Defense-mode runtime image for nautilus-ico.
#
# Differs from upstream's multi-stage Dockerfile: single-stage so the
# FreePascal toolchain (fpc + fp-units-*) stays in the runtime image,
# which is what makes in-container rebuilds possible. The whole defense
# workflow depends on this — see DEFCON plan §Integration tax.
#
# Build context: this directory PLUS vendor/nautilus-finals-2025/ico/.
# The NautilusSource adapter assembles that combined context at
# build_image() time.

FROM --platform=amd64 ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        fp-compiler \
        fp-units-base \
        fp-units-fcl \
        fp-units-gfx \
        openssh-server \
        python3 \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Users ─────────────────────────────────────────────────────────────
#
# `arena_agent` — non-root user the dev container SSHes in as. Owns
# /srv/challenge/ico/. Cannot modify /arena/* (root-owned 555).
#
# sshd's host keys generated at first start (see ENTRYPOINT below).
RUN useradd --create-home --shell /bin/bash arena_agent

# ── Source layout ─────────────────────────────────────────────────────
#
# Bake the source twice:
#   /opt/challenge_src/ico/  — root-owned, read-only template. Used by
#                              entrypoint to repopulate /srv/ on every
#                              container start (clean baseline).
#   /srv/challenge/ico/      — chowned to arena_agent at start. This is
#                              what the agent edits. Per-container
#                              writable layer; no host bind mount.
COPY ico/service/ /opt/challenge_src/ico/
RUN chmod -R 555 /opt/challenge_src/ico

# ── Framework scripts ─────────────────────────────────────────────────
#
# /arena/restart.sh and /arena/checker.sh — invoked by the orchestrator
# via docker exec (as root). Agent SSH user can read+execute, NOT write.
COPY restart.sh /arena/restart.sh
COPY checker.sh /arena/checker.sh
RUN chmod 555 /arena/restart.sh /arena/checker.sh \
    && chown root:root /arena/restart.sh /arena/checker.sh

# ── SSH configuration ─────────────────────────────────────────────────
#
# PermitRootLogin no — non-root only. The agent SSH key is provisioned
# at runtime by ClaudeAgentManager._provision_ssh_pivot, which writes
# /home/arena_agent/.ssh/authorized_keys after the container starts.
RUN mkdir -p /run/sshd /home/arena_agent/.ssh \
    && chown arena_agent:arena_agent /home/arena_agent/.ssh \
    && chmod 700 /home/arena_agent/.ssh \
    && sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

# ── Entrypoint ────────────────────────────────────────────────────────
#
# 1. Generate sshd host keys (needed on first run).
# 2. Copy the read-only source template into the writable workdir,
#    chowned to arena_agent.
# 3. Build the initial ico binary (so the service is ready before the
#    first restart cycle).
# 4. Start sshd. The service itself is launched by /arena/restart.sh
#    on the first MCP restart_service call.
COPY entrypoint.sh /arena/entrypoint.sh
RUN chmod 555 /arena/entrypoint.sh && chown root:root /arena/entrypoint.sh

EXPOSE 4265 22

CMD ["/arena/entrypoint.sh"]
