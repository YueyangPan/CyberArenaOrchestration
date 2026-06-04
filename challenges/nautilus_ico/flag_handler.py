"""IcoFlagHandler — FILE_AT_PATH plant + retrieve for nautilus-ico.

ico reads `/flag` from disk on CONNECT, so planting is a privileged
write to that path. Plant goes via `docker exec` as root inside the
target's prod container (works regardless of cross-team network state).

Retrieve is dual-purpose by design:

  - Own-team verify after plant: `docker exec cat /flag` — uses the same
    privileged channel as plant; quick sanity check the file made it in.
  - Cross-team attack: a real exploit lifts the flag over TCP to port
    4265 (the ico binary's own protocol). That path runs in the attack
    agent's address space, not here — this handler is the orchestrator's
    privileged retrieve, used for own-team verification only.

The "handle" returned by plant is the flag path itself. The flag lives
at a fixed path (one slot per container) so there's no per-flag id; we
return the path so callers don't have to remember it.
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
        # plant/retrieve both go via docker exec; the TCP port is irrelevant
        # for THIS handler. (Attack agents talk to 4265 themselves.)
        return ()

    def plant(self, target: VulboxTarget, flag: str) -> str:
        """Write `flag` (no trailing newline) to /flag inside the
        container. Mode 644 so the service can read it; ownership root
        because the orchestrator's docker exec runs as root."""
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
        """`cat <handle>` from inside the container. Returns the flag,
        or None if the file is missing / unreadable."""
        exec_in = target.meta["exec_in_container"]
        rc, out = exec_in(target.host, f"cat {shlex.quote(handle)}")
        if rc != 0 or not out:
            return None
        return out.strip() or None
