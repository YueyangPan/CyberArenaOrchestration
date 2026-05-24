from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentStatus(str, Enum):
    NOT_INSTALLED = "NOT_INSTALLED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"
    CRASHED = "CRASHED"


@dataclass(frozen=True)
class AgentConfig:
    cli: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    # Text fed to the CLI on stdin (claude reads it as the user prompt in
    # headless mode). Empty → a no-op placeholder is used; smoke tests and
    # M5 prompt-builder override this with concrete instructions.
    system_prompt: str = ""


@dataclass(frozen=True)
class AgentLogs:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class AgentState:
    status: AgentStatus
    pid: int | None = None
    wrapper_pid: int | None = None
    exit_code: int | None = None
