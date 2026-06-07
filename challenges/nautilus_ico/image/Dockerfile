# cyberarena/chal-nautilus-ico runtime image.
#
# Single-stage so the FreePascal toolchain (fpc + fp-units-*) stays in
# the runtime image — the defense workflow rebuilds in-container.
#
# Build context: this dir + vendor/nautilus-finals-2025/ico/. The
# context is assembled by challenges.nautilus_ico.build.build_image().

FROM --platform=linux/amd64 ubuntu:24.04

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

# Two users:
#   arena_agent — non-root user the dev container SSHes in as.
#   ico         — service-runtime user. server.pas calls
#                 DropPrivileges('ico') before handling a connection;
#                 without this user, every child crashes mid-handshake.
RUN useradd --create-home --shell /bin/bash arena_agent \
    && adduser --no-create-home --disabled-password --gecos "" ico

# Bake source twice:
#   /opt/challenge_src/ico/  — read-only template (root-owned 555)
#   /srv/challenge/ico/      — entrypoint cp -r's it per-container,
#                              chowned to arena_agent; agent edits this.
COPY ico/service/ /opt/challenge_src/ico/

# Pre-compile the initial binary at build time (same flags as restart.sh,
# from upstream Makefile). The cp -r in entrypoint brings the binary
# along, so the service is "ready immediately" — no startup fpc cost
# and no per-host compile drift.
RUN cd /opt/challenge_src/ico && \
    fpc -vh -Sd -Un -Cg -Cr- -Co- -O3 -Px86_64 -oico ico.pas

RUN chmod -R 555 /opt/challenge_src/ico

# Framework scripts: root-owned 555. Agent SSH user can read+exec, not write.
COPY restart.sh /arena/restart.sh
COPY checker.sh /arena/checker.sh
RUN chmod 555 /arena/restart.sh /arena/checker.sh \
    && chown root:root /arena/restart.sh /arena/checker.sh

# sshd: non-root only. Agent key is provisioned at install time.
RUN mkdir -p /run/sshd /home/arena_agent/.ssh \
    && chown arena_agent:arena_agent /home/arena_agent/.ssh \
    && chmod 700 /home/arena_agent/.ssh \
    && sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

COPY entrypoint.sh /arena/entrypoint.sh
RUN chmod 555 /arena/entrypoint.sh && chown root:root /arena/entrypoint.sh

EXPOSE 4265 22

CMD ["/arena/entrypoint.sh"]
