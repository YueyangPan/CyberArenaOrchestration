#!/usr/bin/env bash
# Generate sshd host keys, materialize the per-container writable source
# workdir (with the pre-built binary), exec sshd as PID 1.
#
# The ico service itself is launched by /arena/restart.sh on the first
# MCP restart_service call.

set -e

if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi

mkdir -p /srv/challenge/ico
cp -r /opt/challenge_src/ico/. /srv/challenge/ico/
chown -R arena_agent:arena_agent /srv/challenge/ico
chmod -R u+w /srv/challenge/ico

exec /usr/sbin/sshd -D -e
