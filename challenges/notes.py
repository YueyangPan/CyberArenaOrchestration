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

    def is_alive(
        self,
        container: ContainerHandle,
        level: "HealthLevel | None" = None,
    ) -> bool:
        """Multi-level healthcheck.

        - PROCESS: server.py script process is alive (pgrep against the
          script path).
        - TCP: port answers a socket-connect.
        - PROTOCOL: LIST returns 'COUNT '. (Legacy default for M5.)
        - CHECKER: notes has no separable gameplay surface beyond
          PROTOCOL (no vuln, no flag-retrieval path different from PUT/GET);
          documented in the DEFCON plan. Fall through to PROTOCOL.
        """
        # Lazy import to avoid circular with orchestrator.service.
        from challenges.source import HealthLevel

        if level is None:
            level = HealthLevel.PROTOCOL

        c = self._client.containers.get(container.name)

        if level == HealthLevel.PROCESS:
            r = c.exec_run(["pgrep", "-f", SERVER_SCRIPT])
            return r.exit_code == 0

        if level == HealthLevel.TCP:
            probe = (
                f"import socket; s=socket.socket(); s.settimeout(2); "
                f"s.connect(('127.0.0.1', {self._port})); s.close()"
            )
            return c.exec_run(["python3", "-c", probe]).exit_code == 0

        # PROTOCOL + CHECKER both run the LIST handshake.
        probe = (
            f"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {self._port})); s.sendall(b'LIST\\n'); "
            f"print(s.recv(64).decode().strip())"
        )
        result = c.exec_run(["python3", "-c", probe])
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


# ── DEFCON framework: ChallengeSource adapter ──────────────────────────


from challenges.source import ChallengeSource, ChallengeSpec, FlagMechanism  # noqa: E402

# Notes runs in-place inside the shared prod-base image (no per-challenge
# image build; the script is push_archive'd via NotesService.install at
# deploy time). The image tag here just points at the fallback base.
_NOTES_IMAGE_TAG = "cyberarena/prod-base"


class BuiltinNotesSource(ChallengeSource):
    """The built-in notes service as a ChallengeSource.

    Exists so the DEFCON framework's contract test runs the same body
    against `notes` and external adapters — keeping the ABC honest. The
    actual lifecycle is still NotesService (push script, exec server,
    plant via PUT); BuiltinNotesSource is just the discovery shell.

    Defaults to port 5000 to match v1.6's `config/game.yaml` / M5 tests.
    """

    DEFAULT_PORT = 5000

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self._port = port

    def list_challenges(self) -> list[ChallengeSpec]:
        return [
            ChallengeSpec(
                name="notes",
                source="notes",
                port=self._port,
                image_tag=_NOTES_IMAGE_TAG,
                # Notes' plant goes through the service protocol (PUT) —
                # there's no flag file or env var. See FlagMechanism docstring.
                flag_mechanism=FlagMechanism.SERVICE_PROTOCOL,
                source_view=None,  # no defense workflow for the toy challenge
                metadata={
                    # Maps to NotesService's runtime args.
                    "service_class": "challenges.notes:NotesService",
                },
            ),
        ]

    def build_image(self, spec: ChallengeSpec) -> str:
        """Notes ships in the prod-base image (no per-challenge build).

        Returns the existing image tag without touching docker. The
        Orchestrator is expected to already have built `cyberarena/prod-base`
        via scripts/build_images.sh.
        """
        return spec.image_tag