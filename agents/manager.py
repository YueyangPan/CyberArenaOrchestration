"""`AgentManager` ABC — per-CLI recipe + the one-phase dance.

A subclass declares two things about an LLM-agent CLI (Claude Code,
codex, Gemini, …):

  1. `install` — how to install the CLI inside the agent host
  2. `phase_command` — shell argv to invoke it for one phase,
     resuming the prior session

`run_phase` is the shared glue (stage prompt → invoke → kill on
timeout). Concrete subclasses inherit it; no reimplementation needed.

Adding a new CLI = subclass + 2 class constants. The transport handles
host plumbing independently.

`status` / `kill` / `fetch_logs` are deliberately not on the ABC —
they exist only on `ClaudeAgentManager` as legacy smoke-test hooks
and are best-effort given the per-phase invocation model.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import ClassVar

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from .transport import AgentHandle, AgentTransport, CommandResult


class AgentManager(ABC):
    # Path inside the agent host where the per-phase prompt is staged
    # before each invocation. The CLI reads it via stdin redirect (or
    # `--prompt-file` — up to `phase_command` to wire it).
    PROMPT_PATH: ClassVar[str]

    # `pkill -KILL -f <pattern>` fallback if `kill_session` can't find
    # the pidfile (transport-level timeout cleanup). Match a substring
    # unique to this CLI's process tree.
    PHASE_KILL_PATTERN: ClassVar[str]

    @abstractmethod
    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed=None,
    ) -> None:
        """One-shot setup at game start: install the CLI, write env /
        MCP config, provision any SSH pivot the agent needs.
        Idempotent. No process is launched."""
        ...

    @abstractmethod
    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase. Reads the prompt from
        `PROMPT_PATH`; resumes the prior session (`--continue`).

        `session_marker` lets `kill_session` target this run's process
        group via a pidfile (`/tmp/cyberarena_phase_<marker>.pid`).
        Wrap the CLI under `setsid -w` and tee the leader PID into
        that file so the on-timeout kill is non-racy.
        """
        ...

    async def run_phase(
        self,
        transport: AgentTransport,
        handle: AgentHandle,
        prompt: str,
        *,
        timeout: float,
        model: str | None = None,
    ) -> CommandResult:
        """Stage prompt, run CLI with timeout, kill session on timeout.

        Returns the raw `CommandResult`. Persistence + event emission
        (AGENT_COMPLETED / TIMEOUT / FAILED) live in
        `GameMgr.record_phase_done`.
        """
        transport.write_file(handle, self.PROMPT_PATH, prompt.encode())
        session_marker = secrets.token_hex(6)
        result = await transport.run_command(
            handle,
            self.phase_command(model=model, session_marker=session_marker),
            timeout=timeout,
        )
        if result.timed_out:
            # SIGTERM on the docker exec wrapper doesn't always
            # propagate to the in-container child; signal the
            # session's whole process group via the pidfile marker.
            transport.kill_session(handle, session_marker)
        return result
