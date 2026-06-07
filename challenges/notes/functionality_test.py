"""4-level health probe for the notes daemon.

Notes has no separable gameplay surface beyond its protocol, so
CHECKER aliases to PROTOCOL.
"""

from __future__ import annotations

import shlex

from challenges.interface import CheckResult, FunctionalityTest, VulboxTarget

# Path the daemon is baked at inside the image (see image/Dockerfile).
SERVER_SCRIPT_PATH = "/opt/cyberarena/notes/server.py"


class NotesFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str:
        return "notes-functionality"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        exec_in = target.meta["exec_in_container"]
        port = target.ports["service"]

        rc, _ = exec_in(target.host, f"pgrep -f {shlex.quote(SERVER_SCRIPT_PATH)}")
        process = CheckResult(name="process", passed=(rc == 0), detail=f"pgrep rc={rc}")

        tcp_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {port})); s.close()\""
        )
        rc, _ = exec_in(target.host, tcp_probe)
        tcp = CheckResult(name="tcp", passed=(rc == 0), detail=f"socket-connect rc={rc}")

        proto_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {port})); s.sendall(b'LIST\\n'); "
            f"out=s.recv(64).decode(errors='replace').strip(); print(out)\""
        )
        rc, out = exec_in(target.host, proto_probe)
        proto_ok = (rc == 0) and out.strip().startswith("COUNT ")
        protocol = CheckResult(
            name="protocol",
            passed=proto_ok,
            detail=out.strip()[:120] if out else f"rc={rc}",
        )

        checker = CheckResult(
            name="checker",
            passed=proto_ok,
            detail="aliased to PROTOCOL",
        )

        return CheckResult(
            name="notes",
            passed=all(c.passed for c in (process, tcp, protocol, checker)),
            children=[process, tcp, protocol, checker],
        )
