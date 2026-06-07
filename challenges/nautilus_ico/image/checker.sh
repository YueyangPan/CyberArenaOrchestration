#!/usr/bin/env bash
# CHECKER-level health probe for nautilus-ico.
#
# Sends the protocol's CONNECT byte (0x10) and asserts the server replies
# with RESPONSE_ACK (0x00). Smallest probe sufficient to catch
# "cheese-path" attacks where the agent swaps ico for a passive echo
# stub (socat -, netcat -e cat, etc.):
#   real ico       → CONNECT(0x10) → ACK(0x00)
#   echo / socat   → CONNECT(0x10) → 0x10  (echoes the request byte)
#   dead service   → TCP connect fails
#
# Same protections as restart.sh: root-owned 555, agent cannot tamper.
# Exits 0 on pass. stdout/stderr is included in SERVICE_HEALTH_CHECK detail.

set -e

PORT=4265

python3 - "$PORT" <<'EOF'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", port))
except Exception as e:
    print(f"checker: TCP connect failed: {e}", file=sys.stderr)
    sys.exit(1)

CONNECT = b"\x10"
RESPONSE_ACK = b"\x00"

try:
    s.sendall(CONNECT)
    resp = s.recv(1)
except Exception as e:
    print(f"checker: protocol I/O failed: {e}", file=sys.stderr)
    sys.exit(2)

if resp == RESPONSE_ACK:
    print("checker: ico CONNECT handshake ACK'd")
    sys.exit(0)
elif resp == CONNECT:
    print("checker: cheese-path detected (server echoed request byte)", file=sys.stderr)
    sys.exit(3)
else:
    print(f"checker: unexpected response {resp!r}; not ACK and not echo", file=sys.stderr)
    sys.exit(4)
EOF
