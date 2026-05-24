"""AgentManager ABC.

An AgentManager installs, supervises, and tears down one LLM-agent
process inside a team's dev container. Subclasses implement the
flavor-specific install + launch (npm-installed Claude CLI, codex, …);
shared mechanics (supervisor-bash, pid/exit-code files, killpg on the
wrapper's process group) live in the default implementation in claude.py
and are reused by future adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.agent import AgentConfig, AgentLogs, AgentState
from orchestrator.service import ContainerHandle


class AgentManager(ABC):
    @abstractmethod
    def install(self, container: ContainerHandle, cfg: AgentConfig) -> None: ...

    @abstractmethod
    def status(self, container: ContainerHandle) -> AgentState: ...

    @abstractmethod
    def kill(self, container: ContainerHandle) -> None: ...

    @abstractmethod
    def fetch_logs(self, container: ContainerHandle) -> AgentLogs: ...
