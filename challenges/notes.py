"""Notes — the first concrete challenge adapter (M2 demo target).

A tiny TCP "notes" daemon: clients send line-terminated commands and the
server stores text. Ported from the reference repo's challenge_simple
NotesService, adapted to run as a bare python3 process inside our
systemd-less containers.

Protocol (newline-terminated):
  PUT <text>   -> 'OK <id>'
  GET <id>     -> the stored text, or 'ERR'
  LIST         -> 'COUNT <n>'

Used in M2 only to verify the Service lifecycle end-to-end; flag plant /
retrieve flows land in M5 alongside the gameserver scoring loop.
"""

from __future__ import annotations

import io
import tarfile
import textwrap

from orchestrator.service import ContainerHandle, Service

REMOTE_DIR = "/opt/cyberarena/notes"
SERVER_SCRIPT = f"{REMOTE_DIR}/server.py"

_SERVER_SOURCE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import socket, sys, threading

    STORE = []
    LOCK = threading.Lock()

    def handle(conn):
        try:
            conn.settimeout(5)
            data = b''
            while not data.endswith(b'\\n'):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            line = data.decode(errors='replace').strip()
            if line.startswith('PUT '):
                payload = line[4:]
                with LOCK:
                    STORE.append(payload)
                    idx = len(STORE) - 1
                conn.sendall(f'OK {idx}\\n'.encode())
            elif line.startswith('GET '):
                try:
                    idx = int(line[4:])
                    with LOCK:
                        out = STORE[idx]
                    conn.sendall(out.encode() + b'\\n')
                except Exception:
                    conn.sendall(b'ERR\\n')
            elif line == 'LIST':
                with LOCK:
                    n = len(STORE)
                conn.sendall(f'COUNT {n}\\n'.encode())
            else:
                conn.sendall(b'ERR\\n')
        finally:
            conn.close()

    def main(port):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        s.listen(64)
        while True:
            c, _ = s.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    if __name__ == '__main__':
        main(int(sys.argv[1]))
""")


class NotesService(Service):
    """In-place adapter: ships a python script into the container, runs it."""

    def __init__(self, port: int = 5000, client=None) -> None:
        super().__init__(client)
        self._port = port

    @property
    def name(self) -> str:
        return "notes"

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def protocol(self) -> str | None:
        return "notes"

    def install(self, container: ContainerHandle) -> None:
        c = self._client.containers.get(container.name)
        c.exec_run(["mkdir", "-p", REMOTE_DIR])
        tarstream = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode="w") as tar:
            data = _SERVER_SOURCE.encode()
            info = tarfile.TarInfo(name="server.py")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        tarstream.seek(0)
        c.put_archive(REMOTE_DIR, tarstream.read())

    def start(self, container: ContainerHandle) -> None:
        c = self._client.containers.get(container.name)
        c.exec_run(
            ["bash", "-c", f"nohup python3 {SERVER_SCRIPT} {self._port} >/tmp/notes.log 2>&1 &"],
            detach=True,
        )

    def stop(self, container: ContainerHandle) -> None:
        c = self._client.containers.get(container.name)
        c.exec_run(["pkill", "-f", SERVER_SCRIPT])

    def is_alive(self, container: ContainerHandle) -> bool:
        """Protocol-level check: LIST should respond with 'COUNT '."""
        probe = (
            f"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {self._port})); s.sendall(b'LIST\\n'); "
            f"print(s.recv(64).decode().strip())"
        )
        result = self._client.containers.get(container.name).exec_run(
            ["python3", "-c", probe]
        )
        if result.exit_code != 0:
            return False
        out = (result.output or b"").decode(errors="replace").strip()
        return out.startswith("COUNT ")

    def plant_flag(self, container: ContainerHandle, flag: str) -> None:
        """PUT the flag as a note. Notes server replies 'OK <idx>'.

        We connect from inside the container (127.0.0.1) so this works
        regardless of cross-team network state — planting happens at
        game-start and must succeed even before ATTACK opens game_net.
        """
        # repr() handles any quote/backslash in `flag` safely for the python -c body.
        probe = (
            f"import socket, sys\n"
            f"s = socket.socket(); s.settimeout(3)\n"
            f"s.connect(('127.0.0.1', {self._port}))\n"
            f"s.sendall(b'PUT ' + {flag!r}.encode() + b'\\n')\n"
            f"resp = s.recv(64).decode(errors='replace').strip()\n"
            f"print(resp)\n"
            f"sys.exit(0 if resp.startswith('OK ') else 1)\n"
        )
        result = self._client.containers.get(container.name).exec_run(
            ["python3", "-c", probe]
        )
        if result.exit_code != 0:
            out = (result.output or b"").decode(errors="replace").strip()
            raise RuntimeError(
                f"plant_flag failed on {container.name}: rc={result.exit_code}, out={out!r}"
            )