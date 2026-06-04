"""IcoFunctionalityTest — 4-level health probe for nautilus-ico.

Uses the recommended HealthLevel naming convention so the orchestrator
emits one SERVICE_HEALTH_CHECK per level and the panel renders the
familiar 4-chip strip.

ico is a custom Pascal binary with no general-purpose protocol adapter
(no LIST/PUT/GET surface like notes); the gameplay-correctness signal
comes from `/arena/checker.sh`, so PROTOCOL aliases to CHECKER's
outcome rather than running its own probe.
"""

from __future__ import annotations

from challenges.interface import CheckResult, FunctionalityTest, VulboxTarget

PROCESS_PATTERN = "/srv/challenge/ico/ico"
CHECKER_COMMAND = "/arena/checker.sh"


class IcoFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str:
        return "nautilus-ico-functionality"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        exec_in = target.meta["exec_in_container"]
        port = target.ports["service"]

        # PROCESS — pgrep against the launched ico binary path.
        rc, _ = exec_in(target.host, f"pgrep -f {PROCESS_PATTERN!r}")
        process = CheckResult(
            name="process",
            passed=(rc == 0),
            detail=f"pgrep rc={rc}",
        )

        # TCP — does the socket accept on the ico port?
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

        # CHECKER — framework-owned correctness probe. Talks to the live
        # ico over TCP and validates a known interaction. Output truncated
        # so a noisy checker.sh doesn't blow up event payloads.
        rc, out = exec_in(target.host, CHECKER_COMMAND)
        checker_ok = (rc == 0)
        checker_detail = (out.strip() or f"rc={rc}")[-200:]
        checker = CheckResult(
            name="checker",
            passed=checker_ok,
            detail=checker_detail,
        )

        # PROTOCOL — no separable protocol adapter; alias to CHECKER.
        protocol = CheckResult(
            name="protocol",
            passed=checker_ok,
            detail="no protocol adapter; aliased to CHECKER",
        )

        return CheckResult(
            name="nautilus-ico",
            passed=all(c.passed for c in (process, tcp, protocol, checker)),
            detail="",
            children=[process, tcp, protocol, checker],
        )
