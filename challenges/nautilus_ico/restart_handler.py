"""IcoRestartHandler — recompile + respawn ico via /arena/restart.sh.

Defense path: after the agent edits `/srv/challenge/ico/ico.pas`, the
MCP `restart_service` tool invokes this handler. Lifecycle (matches
the script):

  compile — fpc rebuilds the binary from ico.pas
  spawn   — pkill old + nohup new
  smoke   — TCP probe to port 4265 to confirm bind succeeded

Each step becomes one CheckResult leaf so the panel and the agent see
where a defense edit broke (e.g. compile fails -> compile.passed=False
with stderr; spawn-then-bind fails -> smoke.passed=False). The script
itself does compile + spawn together; we observe the script's exit
code as compile/spawn combined, then probe the port as the smoke step.
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

        # compile + spawn: framework-owned script runs as root.
        rc, out = exec_in(target.host, RESTART_COMMAND)
        script_ok = (rc == 0)
        # Split the rc=0 case so panel can tell "compile FAILED" from
        # "spawn FAILED" eventually. For now both leaves share the
        # script's outcome — restart.sh's `set -e` means failure can be
        # in either step and we can't distinguish without parsing stderr.
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

        # smoke: did the new binary actually bind? Only worth checking
        # when the script claimed success; otherwise the port is
        # whatever the prior instance left behind, which is misleading.
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
            smoke = CheckResult(
                name="smoke",
                passed=False,
                detail="skipped; restart.sh failed",
            )

        return CheckResult(
            name="nautilus-ico-restart",
            passed=all(c.passed for c in (compile_, spawn, smoke)),
            detail="",
            children=[compile_, spawn, smoke],
        )
