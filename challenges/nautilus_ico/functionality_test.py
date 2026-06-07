"""4-level health probe for nautilus-ico.

ico has no general protocol adapter (no LIST/PUT/GET), so PROTOCOL
aliases to CHECKER's outcome — `/arena/checker.sh` is the real
gameplay-correctness probe.
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

        rc, _ = exec_in(target.host, f"pgrep -f {PROCESS_PATTERN!r}")
        process = CheckResult(name="process", passed=(rc == 0), detail=f"pgrep rc={rc}")

        tcp_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {port})); s.close()\""
        )
        rc, _ = exec_in(target.host, tcp_probe)
        tcp = CheckResult(name="tcp", passed=(rc == 0), detail=f"socket-connect rc={rc}")

        # Output truncated so a noisy checker.sh doesn't blow up event payloads.
        rc, out = exec_in(target.host, CHECKER_COMMAND)
        checker_ok = (rc == 0)
        checker = CheckResult(
            name="checker",
            passed=checker_ok,
            detail=(out.strip() or f"rc={rc}")[-200:],
        )

        protocol = CheckResult(
            name="protocol",
            passed=checker_ok,
            detail="aliased to CHECKER",
        )

        return CheckResult(
            name="nautilus-ico",
            passed=all(c.passed for c in (process, tcp, protocol, checker)),
            children=[process, tcp, protocol, checker],
        )
