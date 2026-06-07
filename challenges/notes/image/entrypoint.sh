#!/usr/bin/env bash
# Generate sshd host keys on first run, spawn the notes daemon in the
# background, exec sshd in the foreground as PID 1.

set -e

if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi
mkdir -p /run/sshd

nohup python3 /opt/cyberarena/notes/server.py 5000 \
    > /var/log/notes.stdout 2>&1 &

exec /usr/sbin/sshd -D -e
