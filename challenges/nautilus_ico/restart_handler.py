"""Recompile + respawn ico via /arena/restart.sh.

Three leaves: compile + spawn share the script's exit code; smoke is
a TCP probe to confirm the new binary bound the port.
"""

from __future__ import annotations

from challenges.interface import CheckResult, RestartHandler, VulboxTarget

RESTART_COMMAND = "/arena/restart.sh"


class IcoRestartHandler(RestartHandler):
    @property
    def name(self) -> str:
        return "nautilus-ico-restart-handler"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        exec_in = target.meta["exec_in_container"]
        port = target.ports["service"]

        rc, out = exec_in(target.host, RESTART_COMMAND)
        script_ok = (rc == 0)
        # `restart.sh` uses `set -e` so we can't tell compile vs spawn
        # apart from the exit code alone — both leaves share the outcome.
        compile_ = CheckResult(
            name="compile",
            passed=script_ok,
            detail=(out.strip() or f"rc={rc}")[-200:],
        )
        spawn = CheckResult(
            name="spawn",
            passed=script_ok,
            detail="ok" if script_ok else "see compile detail",
        )

        if script_ok:
            tcp_probe = (
                f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
                f"s.connect(('127.0.0.1', {port})); s.close()\""
            )
            srv_rc, _ = exec_in(target.host, tcp_probe)
            smoke = CheckResult(
                name="smoke",
                passed=(srv_rc == 0),
                detail=f"socket-connect rc={srv_rc}",
            )
        else:
            # Probing a dead port would be misleading.
            smoke = CheckResult(
                name="smoke",
                passed=False,
                detail="skipped; restart.sh failed",
            )

        return CheckResult(
            name="nautilus-ico-restart",
            passed=all(c.passed for c in (compile_, spawn, smoke)),
            children=[compile_, spawn, smoke],
        )
