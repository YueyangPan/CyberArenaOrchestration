#!/usr/bin/env bash
# nautilus-ico container entrypoint.
#
# Runs as root: generates sshd host keys (one-time), materializes the
# per-container writable source workdir (private copy of the baked
# template, including the pre-built binary), then exec's sshd in the
# foreground. PID 1 reaps zombies and dies if sshd dies.
#
# The ico service itself is launched by /arena/restart.sh on the first
# MCP restart_service call. Initial `fpc` compile is done at image
# build time (see Dockerfile), not here — the template already carries
# the binary so `cp -r` brings it along.

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

# 3. sshd foreground. restart.sh is invoked on demand via docker exec
#    by the orchestrator for the actual ico service launch + every
#    defense patch cycle.
exec /usr/sbin/sshd -D -e
