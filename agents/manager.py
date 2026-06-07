"""`AgentManager` ABC — per-CLI recipe + the one-phase dance.

A subclass tells the framework two things about one LLM-agent CLI
flavor (Claude Code, OpenAI codex CLI, Gemini CLI, …):

  1. How to install the CLI inside an agent host (`install`)
  2. The shell argv to invoke it for one phase, resuming the prior
     session (`phase_command`)

`run_phase` is the common glue — stage prompt, invoke through the
transport with a timeout, kill the process group on timeout. It lives
here as a concrete method so subclasses don't reimplement it.

Adding a new CLI = subclass with `install` + `phase_command` + two
class constants. No copy-pasted host plumbing; the transport handles
that axis independently.

`status` / `kill` / `fetch_logs` from the M4-era observability surface
are not abstract here — concrete subclasses may keep them for legacy
smoke tests, but new CLIs don't need them (per-phase invocation makes
them best-effort no-ops anyway).
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
        """Shell argv that drives one phase.

        Reads the prompt from `PROMPT_PATH` and resumes the prior
        session (`--continue` / `--resume` / equivalent).

        `session_marker` is the per-invocation token used by
        `kill_session` to target this run's process group via a
        pidfile (typically `/tmp/cyberarena_phase_<marker>.pid`).
        Wrap claude under `setsid -w` and tee the leader's PID into
        that file so the kill on timeout is non-racy.
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
        """Run one phase via `transport`. Returns the raw `CommandResult`.

        Stages the prompt, runs the CLI with `timeout`, and on timeout
        precision-kills the session via the marker-pidfile pattern.
        Persistence (stream-{phase}.jsonl etc.) and event emission
        (AGENT_COMPLETED / TIMEOUT / FAILED) are the caller's job —
        see `GameMgr.record_phase_done`.
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
