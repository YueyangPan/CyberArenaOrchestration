"""FILE_AT_PATH flag handler for nautilus-ico.

ico reads `/flag` on CONNECT, so planting is a privileged write to that
path via docker exec. Retrieve `cat /flag` is the orchestrator's
own-team verification path; cross-team attacks lift the flag over TCP
4265 (the ico binary's own protocol), which lives in the agent's code.
"""

from __future__ import annotations

import shlex

from challenges.interface import FlagHandler, VulboxTarget

FLAG_PATH = "/flag"


class IcoFlagHandler(FlagHandler):
    @property
    def name(self) -> str:
        return "nautilus-ico-flag-handler"

    @property
    def required_ports(self) -> tuple[str, ...]:
        # plant/retrieve go via docker exec; the TCP port isn't this
        # handler's concern (attack agents talk to 4265 themselves).
        return ()

    def plant(self, target: VulboxTarget, flag: str) -> str:
        """Write `flag` (no trailing newline) to /flag, mode 644."""
        exec_in = target.meta["exec_in_container"]
        cmd = (
            f"sh -c 'printf %s {shlex.quote(flag)} > {shlex.quote(FLAG_PATH)} "
            f"&& chmod 644 {shlex.quote(FLAG_PATH)}'"
        )
        rc, out = exec_in(target.host, cmd)
        if rc != 0:
            raise RuntimeError(
                f"nautilus-ico plant_flag failed on {target.host}: rc={rc}, out={out!r}"
            )
        return FLAG_PATH

    def retrieve(self, target: VulboxTarget, handle: str) -> str | None:
        exec_in = target.meta["exec_in_container"]
        rc, out = exec_in(target.host, f"cat {shlex.quote(handle)}")
        if rc != 0 or not out:
            return None
        return out.strip() or None
