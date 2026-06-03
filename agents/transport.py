"""AgentTransport — pluggable execution backend for the agent host.

`ClaudeAgentManager` never reaches for `docker exec` directly. All
side effects on the agent host (running commands, writing files,
killing processes) go through this interface. Two implementations
live as siblings:

  - DockerExecBackend  (agents/transport_docker.py)  — local Docker
  - SSHBackend         (deferred)                    — remote VM / AWS

See docs/persistent_memory_agent_plan_v1.md for the design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.service import ContainerHandle


# AgentHandle is opaque to the manager; each transport interprets it.
# For DockerExecBackend it's ContainerHandle (only `.name` is used).
# SSHBackend would carry host/user/key. Aliased here so manager-side
# code can type-hint `AgentHandle` without importing ContainerHandle.
AgentHandle = ContainerHandle


@dataclass(frozen=True)
class CommandResult:
    """Returned by AgentTransport.run_command. `timed_out=True` means
    the backend already escalated SIGTERM → SIGKILL; stdout/stderr
    are whatever was captured up to that point."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class AgentTransport(Protocol):
    """One-host execution surface. Implementations are stateless across
    handles (the same transport instance handles every team)."""

    async def connect(self, handle: AgentHandle) -> None:
        """One-time transport setup. No-op for docker; opens a control
        master / persistent connection for ssh."""
        ...

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
        """Run `cmd` on the agent host. Returns exit/stdout/stderr/timed_out.
        Backend handles its own SIGTERM → SIGKILL escalation on timeout."""
        ...

    def write_file(
        self,
        handle: AgentHandle,
        remote_path: str,
        content: bytes,
    ) -> None:
        """Write content to a path on the agent host. Idempotent."""
        ...

    def read_file(self, handle: AgentHandle, remote_path: str) -> bytes:
        """Read a file from the agent host. Used to pull artifacts when
        the backend can't return them inline via run_command stdout."""
        ...

    def kill_process(self, handle: AgentHandle, pattern: str) -> None:
        """Force-kill processes matching pattern (pgrep-style). Used by
        teardown paths (e.g. ClaudeAgentManager.kill) that don't have a
        specific PID. Best-effort: never raises.

        Prefer `kill_session` from the per-phase runner — `kill_process`
        races when the same container hosts multiple phases in sequence
        (a freshly-started next-phase claude matches the same pattern).
        """
        ...

    def kill_session(self, handle: AgentHandle, session_marker: str) -> None:
        """Kill the specific process whose PID was written to
        `/tmp/cyberarena_phase_<marker>.pid` by phase_command's bash
        wrapper. No-op (best-effort) when the file is missing or empty.

        Replaces the racy pattern-based `kill_process` for the runner's
        on-timeout pkill: targets exactly the claude instance started
        under this session marker, not a sibling that just started in
        the next phase."""
        ...
