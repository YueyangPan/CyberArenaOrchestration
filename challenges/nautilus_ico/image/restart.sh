#!/usr/bin/env bash
# Framework restart script for nautilus-ico.
#
# Invoked by the MCP `restart_service` tool via docker exec (root).
# Mode 555, root-owned — the agent can read + execute but not modify.
# The only path from agent edit to running binary is this script.

set -e

SRC_DIR="/srv/challenge/ico"
BIN="${SRC_DIR}/ico"
LOG="/var/log/ico.stdout"

cd "${SRC_DIR}"

# Same flags as the build-time pre-compile (upstream Makefile release config).
fpc -vh -Sd -Un -Cg -Cr- -Co- -O3 -Px86_64 -oico ico.pas

# Kill any prior instance — fresh container has none, hence `|| true`.
pkill -f "${BIN}" || true

# Logs to a fixed path the SERVICE_LOG artifact hook reads.
mkdir -p "$(dirname "${LOG}")"
nohup "${BIN}" > "${LOG}" 2>&1 &

# Brief settle so a TCP probe right after restart sees the bound port.
sleep 0.5

echo "ico restarted, pid=$(pgrep -f "${BIN}" | head -1)"
