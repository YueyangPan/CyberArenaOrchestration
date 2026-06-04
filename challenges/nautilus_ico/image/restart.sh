#!/usr/bin/env bash
# Framework restart script for nautilus-ico.
#
# Invoked by the MCP `restart_service` tool via `docker exec` from the
# orchestrator (which runs as root inside the container). Agent SSH user
# (arena_agent, non-root) can read + execute this script but CANNOT
# write it — owner root:root, mode 555. The agent's only path to
# trigger a restart is the MCP tool, which the orchestrator mediates.
#
# Lifecycle:
#   1. cd into agent's writable source dir
#   2. Re-compile via fpc (flags lifted from upstream Makefile release config)
#   3. Kill the running ico if present
#   4. Re-launch the new binary detached, redirecting stdout/stderr

set -e

SRC_DIR="/srv/challenge/ico"
BIN="${SRC_DIR}/ico"
LOG="/var/log/ico.stdout"

cd "${SRC_DIR}"

# Upstream Makefile uses: fpc -vh -Sd -Un -Cg -Cr- -Co- -O3 -Px86_64 -oico ico.pas
# Same flags here so we match production-mode optimization + warnings.
fpc -vh -Sd -Un -Cg -Cr- -Co- -O3 -Px86_64 -oico ico.pas

# Kill any prior instance. `|| true` because a fresh install has nothing
# to kill and that's not a failure.
pkill -f "${BIN}" || true

# Launch detached, logs to a fixed path the SERVICE_LOG artifact hook reads.
mkdir -p "$(dirname "${LOG}")"
nohup "${BIN}" > "${LOG}" 2>&1 &

# Brief wait so a TCP probe right after restart_cmd returns has a chance
# to see the port open. The orchestrator's PROCESS/TCP health checks
# will retry, but this shortens the happy path.
sleep 0.5

echo "ico restarted, pid=$(pgrep -f "${BIN}" | head -1)"
