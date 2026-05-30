"""Persistent-memory v1 — DockerExecBackend unit tests.

Mocks `asyncio.create_subprocess_exec` so we don't need a real docker
daemon. Pins:
  ①run_command → CommandResult fields (exit_code, stdout, stderr,
    timed_out) under happy / timeout / non-zero paths.
  ②argv composition: `docker exec -i <name> <cmd...>` for list cmd,
    `docker exec -i <name> bash -c "..."` when cwd / env / str cmd.
  ③write_file uses docker-py put_archive (binary-safe tar wrapping).
  ④kill_process delegates to `pkill -KILL -f <pattern>`.

See docs/persistent_memory_agent_plan_v1.md.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from agents.transport import CommandResult
from agents.transport_docker import DockerExecBackend
from orchestrator.service import ContainerHandle


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="x", name=name, networks=[])


class _FakeProc:
    """asyncio.subprocess.Process stand-in. Returns canned communicate()
    output; supports terminate()/kill() by recording the call and
    completing communicate() on a follow-up await."""

    def __init__(self, *, returncode: int, stdout: bytes, stderr: bytes, hang: bool = False):
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.returncode = returncode if not hang else None
        self.terminated = False
        self.killed = False
        self._after_signal = False

    async def communicate(self, input=None):
        if self._hang and not self._after_signal:
            # First call: never completes — emulate a hanging process.
            await asyncio.sleep(3600)
        # After terminate()/kill() we settle.
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True
        self.returncode = 143  # SIGTERM
        self._after_signal = True

    def kill(self):
        self.killed = True
        self.returncode = 137  # SIGKILL
        self._after_signal = True


def _patch_subprocess(proc):
    """Decorator-free helper: returns the mock factory + patcher context."""
    async def _factory(*args, **kwargs):
        return proc
    return patch("asyncio.create_subprocess_exec", side_effect=_factory)


# ── run_command: happy path ───────────────────────────────────────────


def test_run_command_returns_canonical_result():
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"hello\n", stderr=b"")

    async def go():
        with _patch_subprocess(proc):
            return await backend.run_command(_handle(), ["echo", "hello"])
    r = asyncio.run(go())

    assert isinstance(r, CommandResult)
    assert r.exit_code == 0
    assert r.stdout == b"hello\n"
    assert r.stderr == b""
    assert r.timed_out is False


def test_run_command_list_form_does_not_wrap_in_bash():
    """For a plain list cmd with no cwd/env, argv is `docker exec -i NAME <cmd...>`."""
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    seen = {}

    async def _factory(*args, **kwargs):
        seen["argv"] = args
        return proc

    async def go():
        with patch("asyncio.create_subprocess_exec", side_effect=_factory):
            await backend.run_command(_handle(), ["echo", "hi"])
    asyncio.run(go())

    argv = list(seen["argv"])
    assert argv[:4] == ["docker", "exec", "-i", "team1_dev"]
    assert argv[4:] == ["echo", "hi"]


def test_run_command_with_cwd_wraps_in_bash():
    """cwd forces a bash -c invocation with `cd <cwd> && <cmd>`."""
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    seen = {}

    async def _factory(*args, **kwargs):
        seen["argv"] = args
        return proc

    async def go():
        with patch("asyncio.create_subprocess_exec", side_effect=_factory):
            await backend.run_command(
                _handle(), ["echo", "ok"], cwd="/tmp/work",
            )
    asyncio.run(go())

    argv = list(seen["argv"])
    assert argv[:4] == ["docker", "exec", "-i", "team1_dev"]
    assert argv[4:6] == ["bash", "-c"]
    shell = argv[6]
    assert "cd /tmp/work" in shell or "cd '/tmp/work'" in shell
    assert "echo ok" in shell


def test_run_command_string_cmd_goes_through_bash():
    """A `str` cmd is shell input; the backend should bash -c it."""
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    seen = {}

    async def _factory(*args, **kwargs):
        seen["argv"] = args
        return proc

    async def go():
        with patch("asyncio.create_subprocess_exec", side_effect=_factory):
            await backend.run_command(_handle(), "echo hi | cat")
    asyncio.run(go())

    argv = list(seen["argv"])
    assert argv[4:6] == ["bash", "-c"]
    assert argv[6] == "echo hi | cat"


# ── run_command: timeout escalation ───────────────────────────────────


def test_run_command_timeout_terminates_and_returns_timed_out():
    """On wait_for timeout the backend SIGTERMs and returns timed_out=True
    with whatever stdout/stderr we observed (here: empty since the proc
    hung before flushing)."""
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"", stderr=b"", hang=True)

    async def go():
        with _patch_subprocess(proc):
            return await backend.run_command(_handle(), ["sleep", "60"], timeout=0.05)
    r = asyncio.run(go())

    assert r.timed_out is True
    assert proc.terminated is True
    # exit_code = SIGTERM rc (143). The hangProc settles after terminate.
    assert r.exit_code == 143


# ── write_file / read_file: docker-py archive paths ───────────────────


def test_write_file_uses_put_archive_with_tar():
    """write_file builds a single-entry tar and ships it via put_archive
    rooted at the parent dir. Binary-safe; no shell quoting."""
    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    backend.write_file(_handle(), "/var/lib/cyberarena/prompt.txt", b"hello\xff\x00")

    # mkdir -p the parent
    mkdir_calls = [
        c for c in container.exec_run.call_args_list
        if c.args and c.args[0] == ["mkdir", "-p", "/var/lib/cyberarena"]
    ]
    assert mkdir_calls, "expected mkdir -p of parent dir"

    # put_archive called with tar bytes containing prompt.txt
    assert container.put_archive.called
    parent_arg, tarbytes = container.put_archive.call_args.args
    assert parent_arg == "/var/lib/cyberarena"
    with tarfile.open(fileobj=io.BytesIO(tarbytes)) as tar:
        members = tar.getmembers()
        assert len(members) == 1
        assert members[0].name == "prompt.txt"
        f = tar.extractfile(members[0])
        assert f is not None
        assert f.read() == b"hello\xff\x00"


def test_read_file_unwraps_tar_from_get_archive():
    container = MagicMock()
    # Build a synthetic tar stream that get_archive would return.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"world"))
    container.get_archive.return_value = (iter([buf.getvalue()]), {"size": 5})

    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    out = backend.read_file(_handle(), "/var/lib/cyberarena/hello.txt")
    assert out == b"world"


# ── kill_process ──────────────────────────────────────────────────────


def test_kill_process_runs_pkill():
    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    backend.kill_process(_handle(), "claude -p")

    assert any(
        c.args and c.args[0] == ["pkill", "-KILL", "-f", "claude -p"]
        for c in container.exec_run.call_args_list
    ), container.exec_run.call_args_list


def test_kill_process_is_best_effort_swallows_exceptions():
    client = MagicMock()
    client.containers.get.side_effect = RuntimeError("docker is down")
    backend = DockerExecBackend(client=client)

    # Should not raise.
    backend.kill_process(_handle(), "claude -p")
