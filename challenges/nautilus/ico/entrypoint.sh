#!/usr/bin/env bash
# nautilus-ico container entrypoint.
#
# Runs as root, sets up the per-container source layout (private copy of
# the baked template), generates sshd host keys, builds the initial
# binary, then starts sshd in the foreground. The ico service itself is
# launched by /arena/restart.sh on the first MCP restart_service call —
# so the same restart path runs at start-of-game and at every defense
# round.

set -e

# 1. sshd host keys (one-time at first container start).
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi

# 2. Materialize the writable source workdir from the read-only template.
#    Each container gets its own private copy — no host bind, no cross-team aliasing.
mkdir -p /srv/challenge/ico
cp -r /opt/challenge_src/ico/. /srv/challenge/ico/
chown -R arena_agent:arena_agent /srv/challenge/ico
chmod -R u+w /srv/challenge/ico

# 3. Initial build (same flags as restart.sh).
cd /srv/challenge/ico
fpc -vh -Sd -Un -Cg -Cr- -Co- -O3 -Px86_64 -oico ico.pas || {
    echo "initial fpc build failed — container will still start so the orchestrator can inspect" >&2
}

# 4. Start sshd in the foreground. PID 1 reaps zombies and dies if sshd dies.
#    `restart.sh` is invoked on demand via `docker exec` for the actual
#    service launch + every patch cycle.
exec /usr/sbin/sshd -D -e
