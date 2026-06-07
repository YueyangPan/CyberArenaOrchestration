# cyberarena/chal-notes runtime image.
#
# ubuntu + python3 + sshd. Bakes server.py + an arena_agent SSH user.
# Entrypoint runs sshd in the foreground and spawns the daemon in the
# background — no docker-exec ship-and-spawn at deploy time.

FROM --platform=linux/amd64 ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        openssh-server \
        ca-certificates \
        procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root user the dev container SSHes in as. The framework's
# _provision_ssh_pivot auto-detects this via `id arena_agent`.
RUN useradd --create-home --shell /bin/bash arena_agent

# sshd: non-root only. The agent key is provisioned at install time.
RUN mkdir -p /run/sshd /home/arena_agent/.ssh \
    && chown arena_agent:arena_agent /home/arena_agent/.ssh \
    && chmod 700 /home/arena_agent/.ssh \
    && sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

# Daemon source, read-only. The functionality_test pgreps for this exact path.
RUN mkdir -p /opt/cyberarena/notes
COPY server.py /opt/cyberarena/notes/server.py
RUN chmod 555 /opt/cyberarena/notes/server.py

COPY entrypoint.sh /arena/entrypoint.sh
RUN chmod 555 /arena/entrypoint.sh

EXPOSE 22 5000
ENTRYPOINT ["/arena/entrypoint.sh"]
