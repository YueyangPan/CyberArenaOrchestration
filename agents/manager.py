"""AgentManager ABC — per-CLI recipe contract.

An `AgentManager` subclass tells the framework two things about one
LLM-agent CLI flavor (Claude Code, OpenAI codex CLI, Gemini CLI, …):

  1. How to install the CLI inside an agent host
  2. The shell argv to invoke that CLI for one phase with
     persistent-memory semantics (resume previous session via the
     CLI's own `--continue` / `--resume` flag)

Everything else — writing the prompt file, awaiting the transport,
killing on timeout, persisting artifacts, emitting events — lives in
`agents.runner.run_phase_via_transport` + `GameMgr.record_phase_*`,
not on the manager. The split means:

  * Adding a new CLI = subclass with `install` + `phase_command` +
    two class constants, ~50-80 LOC. No copy-pasted host plumbing.
  * Swapping host transports = backend change, not a manager change
    (see `agents.transport`).

`status` / `kill` / `fetch_logs` from the M4-era observability surface
are NOT abstract here. Concrete subclasses may keep them for legacy
live-smoke tests, but new CLIs are not required to implement them —
their semantics (per-phase invocation, not a long-running supervisor)
mean status/kill/fetch_logs are best-effort no-ops anyway.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from models.agent import AgentConfig
from orchestrator.service import ContainerHandle


class AgentManager(ABC):
    # File path inside the agent host where the per-phase prompt is
    # written before each invocation. The CLI consumes it via stdin
    # redirect (or `--prompt-file` if the CLI prefers that — implement
    # however you want in `phase_command`).
    PROMPT_PATH: ClassVar[str]

    # `pkill -KILL -f <pattern>` is run inside the host after a
    # `transport.run_command` timeout if SIGTERM didn't propagate
    # cleanly. Should match a substring unique to this CLI's process
    # tree so we don't pkill the agent's own tool subprocesses.
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
        MCP config files, provision any SSH pivot the agent needs.
        Idempotent. No process is launched."""
        ...

    @abstractmethod
    def phase_command(self, *, model: str | None = None) -> list[str]:
        """Shell argv to invoke the CLI for one phase. Must read the
        prompt from `PROMPT_PATH` and resume the prior session so
        memory carries (`--continue` / `--resume` / equivalent).

        Return type matches `AgentTransport.run_command`'s `cmd`
        argument: list of argv tokens. For shell-form invocations
        (cd / source / redirect), wrap in `["bash", "-c", "..."]`.
        """
        ...
