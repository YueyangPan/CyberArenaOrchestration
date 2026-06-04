#!/usr/bin/env bash
# Notes container entrypoint.
#
# Same pattern as the ico image: prep one-shot state (sshd host keys),
# launch the gameplay daemon in the background, exec sshd in the
# foreground so PID 1 reaps zombies and the container dies if sshd
# dies.

set -e

# sshd host keys (one-time at first container start).
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi
mkdir -p /run/sshd

# Notes daemon — listens on the port baked into the image (5000 for the
# v1 game). Detached + logged to a fixed path so an operator can tail
# the file via docker exec for debugging.
nohup python3 /opt/cyberarena/notes/server.py 5000 \
    > /var/log/notes.stdout 2>&1 &

# sshd in the foreground — PID 1 reaps zombies and dies if sshd dies.
exec /usr/sbin/sshd -D -e
