"""AgentManager ABC.

An AgentManager installs and per-phase invokes one LLM-agent on the
agent host (dev container today, remote VM tomorrow via SSHBackend).
Subclasses implement the flavor-specific install + per-phase invocation
(npm-installed Claude CLI today, codex/other CLIs tomorrow); shared
mechanics live in the default implementation in claude.py and are
reused by future adapters.

Persistent-memory v1 (docs/persistent_memory_agent_plan_v1.md):
  - install() is one-shot at game start (no supervisor launched).
  - run_phase() runs the agent for one phase via the transport and
    returns a DoneInfo. Memory carries across phases via the CLI's
    own session store on disk (Claude's `--continue`).
  - status() / kill() / fetch_logs() remain for M4-era observability
    and are best-effort no-ops under v1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from models.agent import AgentConfig, AgentLogs, AgentState, DoneInfo
from orchestrator.service import ContainerHandle


class AgentManager(ABC):
    @abstractmethod
    def install(self, container: ContainerHandle, cfg: AgentConfig) -> None: ...

    @abstractmethod
    async def run_phase(
        self,
        container: ContainerHandle,
        prompt: str,
        *,
        round_id: int,
        phase: str,
        timeout: float,
        artifact_dir: Path | None = None,
        model: str | None = None,
    ) -> DoneInfo:
        """Run one phase: deliver `prompt`, invoke the underlying CLI on
        the agent host through the transport, capture stdout/stderr +
        per-phase artifacts, return a DoneInfo.

        Implementations must:
          - honor `timeout` and escalate SIGTERM → SIGKILL on expiry
            (set `timed_out=True` in the returned DoneInfo);
          - write `stream-{phase}.jsonl`, `agent-{phase}.log`, and
            `metadata-{phase}.json` into `artifact_dir` when supplied;
          - never raise on agent-side failures — surface them via
            `exit_code` / `stderr` in the returned DoneInfo.
        """
        ...

    @abstractmethod
    def status(self, container: ContainerHandle) -> AgentState: ...

    @abstractmethod
    def kill(self, container: ContainerHandle) -> None: ...

    @abstractmethod
    def fetch_logs(self, container: ContainerHandle) -> AgentLogs: ...
