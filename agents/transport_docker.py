"""DockerExecBackend — AgentTransport implementation against a local
Docker daemon.

`run_command` shells out to `docker exec` via `asyncio.create_subprocess_exec`
so the orchestrator's FastAPI event loop stays free to serve the
agent's MCP tool calls during a long-running phase. `write_file` and
`kill_process` use the same approach for consistency (everything is
one `docker exec` call away).

See docs/persistent_memory_agent_plan_v1.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex

import docker

from .transport import AgentHandle, AgentTransport, CommandResult

log = logging.getLogger(__name__)


class DockerExecBackend(AgentTransport):
    """`docker exec`-based AgentTransport. Stateless; one instance handles
    every team's container.
    """

    # Grace period between SIGTERM and SIGKILL when run_command times out.
    KILL_GRACE_SECS = 2.0

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        # docker-py client is used for file I/O (put_archive / get_archive)
        # which is binary-safe and avoids the shell quoting hazards of
        # piping bytes through `docker exec -i`.
        self._client = client or docker.from_env()

    async def connect(self, handle: AgentHandle) -> None:
        # docker exec is stateless; no per-handle setup needed. The
        # SSHBackend equivalent would open a control-master connection.
        return None

    async def run_command(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run `cmd` via `docker exec`. On timeout: terminate → kill →
        pkill belt-and-suspenders.

        cwd is implemented by prepending `cd <cwd> && ` to a bash -c
        invocation. env is implemented by prepending `K=V ...` (NOT
        `--env` to docker exec, which would require the variables to be
        defined in the container's environment file). Both are best-
        effort conveniences; callers that need richer setup should bake
        it into `cmd` directly.
        """
        argv = self._build_argv(handle, cmd, cwd=cwd, env=env)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            # SIGTERM the docker exec subprocess; it tries to propagate
            # to the in-container child. May or may not work cleanly.
            proc.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.KILL_GRACE_SECS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()

        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
            timed_out=timed_out,
        )

    def write_file(
        self,
        handle: AgentHandle,
        remote_path: str,
        content: bytes,
    ) -> None:
        """Write content into the container via docker-py's put_archive
        (binary-safe; no shell quoting). Creates parent dirs as needed."""
        import io
        import tarfile

        parent, fname = os.path.split(remote_path)
        if parent:
            # Ensure parent exists. Idempotent.
            c = self._client.containers.get(handle.name)
            c.exec_run(["mkdir", "-p", parent])

        tarstream = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode="w") as tar:
            info = tarfile.TarInfo(name=fname)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        tarstream.seek(0)

        c = self._client.containers.get(handle.name)
        c.put_archive(parent or "/", tarstream.read())

    def read_file(self, handle: AgentHandle, remote_path: str) -> bytes:
        """Read a file from the container via docker-py's get_archive.
        Returns the file's raw bytes (the tarball wrapping is unwrapped)."""
        import io
        import tarfile

        c = self._client.containers.get(handle.name)
        stream, _ = c.get_archive(remote_path)
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            members = tar.getmembers()
            if not members:
                return b""
            f = tar.extractfile(members[0])
            return f.read() if f else b""

    def kill_process(self, handle: AgentHandle, pattern: str) -> None:
        """Best-effort pkill inside the container. Used after a
        run_command timeout if SIGTERM didn't propagate through
        docker exec to the actual claude child process."""
        try:
            c = self._client.containers.get(handle.name)
            c.exec_run(["pkill", "-KILL", "-f", pattern])
        except Exception:
            log.exception("kill_process failed on %s pattern=%r", handle.name, pattern)

    # ── internals ───────────────────────────────────────────────────────

    def _build_argv(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> list[str]:
        """Compose the full `docker exec` argv. For str cmd we go through
        bash -c (single command line); for list cmd we hand each token
        through individually unless cwd/env force a shell."""
        prefix: list[str] = []
        if env:
            prefix = [f"{k}={shlex.quote(v)}" for k, v in env.items()]

        if cwd or env or isinstance(cmd, str):
            if isinstance(cmd, list):
                shell = " ".join(shlex.quote(a) for a in cmd)
            else:
                shell = cmd
            if cwd:
                shell = f"cd {shlex.quote(cwd)} && {shell}"
            if prefix:
                shell = " ".join(prefix) + " " + shell
            return ["docker", "exec", "-i", handle.name, "bash", "-c", shell]
        return ["docker", "exec", "-i", handle.name, *cmd]
