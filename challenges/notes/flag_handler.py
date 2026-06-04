"""NotesFlagHandler — plant/retrieve flags via the notes daemon's own protocol.

`plant` does `PUT <flag>` from inside the prod container (127.0.0.1)
so it works regardless of cross-team network state — planting happens
at game start and must succeed even before ATTACK opens game_net. The
daemon replies `OK <id>`; we parse and return `<id>` as the opaque
handle. `retrieve` then does `GET <id>`.

Notes is attack-only in the v1 game.yaml so no defense rebuild
exists, but the FlagHandler is still wired the same way attack agents
would use over the public interface — same code path with a different
target.
"""

from __future__ import annotations

from challenges.interface import FlagHandler, VulboxTarget


class NotesFlagHandler(FlagHandler):
    @property
    def name(self) -> str:
        return "notes-flag-handler"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def plant(self, target: VulboxTarget, flag: str) -> str:
        """PUT the flag, return the `<id>` from the daemon's `OK <id>` reply."""
        exec_in = target.meta["exec_in_container"]
        port = target.ports["service"]
        # repr(flag) is python-safe for the python -c body — handles
        # quote / backslash / unicode in the flag string.
        probe = (
            f"import socket, sys\n"
            f"s = socket.socket(); s.settimeout(3)\n"
            f"s.connect(('127.0.0.1', {port}))\n"
            f"s.sendall(b'PUT ' + {flag!r}.encode() + b'\\n')\n"
            f"resp = s.recv(64).decode(errors='replace').strip()\n"
            f"print(resp)\n"
            f"sys.exit(0 if resp.startswith('OK ') else 1)\n"
        )
        rc, out = exec_in(target.host, f"python3 -c {_q(probe)}")
        if rc != 0:
            raise RuntimeError(
                f"notes plant_flag failed on {target.host}: rc={rc}, out={out!r}"
            )
        # Reply shape: "OK <idx>" — return idx as the handle.
        line = out.strip().splitlines()[-1] if out else ""
        parts = line.split()
        if len(parts) < 2 or parts[0] != "OK":
            raise RuntimeError(
                f"notes plant_flag: unexpected reply on {target.host}: {line!r}"
            )
        return parts[1]

    def retrieve(self, target: VulboxTarget, handle: str) -> str | None:
        """GET <handle>. Returns the stored flag, or None if the daemon
        answered ERR (id out of range, store rotated since plant, etc.)."""
        exec_in = target.meta["exec_in_container"]
        port = target.ports["service"]
        probe = (
            f"import socket\n"
            f"s = socket.socket(); s.settimeout(3)\n"
            f"s.connect(('127.0.0.1', {port}))\n"
            f"s.sendall(b'GET {handle}\\n')\n"
            f"print(s.recv(4096).decode(errors='replace'), end='')\n"
        )
        rc, out = exec_in(target.host, f"python3 -c {_q(probe)}")
        if rc != 0:
            return None
        line = out.strip().splitlines()[-1] if out else ""
        if line == "ERR" or not line:
            return None
        return line


def _q(s: str) -> str:
    """Single-quote the python source for bash -c. Bash single quotes
    can't contain a literal single quote, so we escape with the
    '\\'' trick — standard shell idiom."""
    return "'" + s.replace("'", "'\\''") + "'"
