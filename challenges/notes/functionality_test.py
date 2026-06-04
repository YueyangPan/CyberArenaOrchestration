"""NotesFunctionalityTest — 4-level health probe for the notes daemon.

Uses the recommended HealthLevel naming convention so the orchestrator
emits one SERVICE_HEALTH_CHECK per level and the panel renders the
familiar 4-chip strip.

Notes has no deeper gameplay surface than its protocol (no
exploitable vuln in the spec, no flag-retrieval path different from
PUT/GET) so the CHECKER level aliases to PROTOCOL — documented in the
DEFCON v1 plan.
"""

from __future__ import annotations

import shlex

from challenges.interface import CheckResult, FunctionalityTest, VulboxTarget

# Runtime path the daemon is baked at in the cyberarena/chal-notes
# image (see challenges/notes/image/Dockerfile). Used by the PROCESS-
# level pgrep here. Kept inline so the only out-of-tree consumer of the
# path lives next to the only in-tree consumer.
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

        # PROCESS — pgrep against the script path the deploy step wrote.
        rc, _ = exec_in(target.host, f"pgrep -f {shlex.quote(SERVER_SCRIPT_PATH)}")
        process = CheckResult(
            name="process",
            passed=(rc == 0),
            detail=f"pgrep rc={rc}",
        )

        # TCP — does the socket accept?
        tcp_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {port})); s.close()\""
        )
        rc, _ = exec_in(target.host, tcp_probe)
        tcp = CheckResult(
            name="tcp",
            passed=(rc == 0),
            detail=f"socket-connect rc={rc}",
        )

        # PROTOCOL — speak notes' newline-terminated protocol.
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

        # CHECKER — notes has no separable gameplay surface; alias.
        checker = CheckResult(
            name="checker",
            passed=proto_ok,
            detail="no separable checker; aliased to PROTOCOL",
        )

        return CheckResult(
            name="notes",
            passed=all(c.passed for c in (process, tcp, protocol, checker)),
            detail="",
            children=[process, tcp, protocol, checker],
        )
