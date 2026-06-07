"""`Challenge` ABC — the unit one CTF problem packages itself as."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .probe import FlagHandler, FunctionalityTest, RestartHandler


@dataclass(frozen=True)
class VulboxImage:
    """Docker image that runs the vulnerable app.

    Fields:
      reference     — image tag (registry pull or local build)
      exposed_ports — name -> container port; names must match the paired
                      probe's `required_ports`
      env           — default env vars baked into deployment
      build_context — path to a Dockerfile dir; if set, the orchestrator
                      builds before deploying. None = pull from registry.
    """

    reference: str
    exposed_ports: dict[str, int]
    env: dict[str, str] = field(default_factory=dict)
    build_context: str | None = None


@dataclass(frozen=True)
class ChallengeDocs:
    """Authoring-time docs bundled with a challenge.

      intro          — human-facing description (dashboard / writeups)
      agent_prompts  — role-keyed prompts surfaced to autonomous agents
                       (unused in the current dispatcher — kept for plugins)
      func_test_spec — human-readable spec for what FunctionalityTest checks
    """

    intro: str
    agent_prompts: dict[str, str]
    func_test_spec: str


class Challenge(ABC):
    """One CTF challenge, packaged for the orchestrator.

    The same instance is reused across teams — per-team state goes on the
    `VulboxTarget` passed to probes at call time, never on `self`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable kebab-case id (`"notes"`, `"nautilus-ico"`)."""
        ...

    @property
    @abstractmethod
    def vulbox(self) -> VulboxImage: ...

    @property
    @abstractmethod
    def functionality_test(self) -> FunctionalityTest: ...

    @property
    @abstractmethod
    def flag_handler(self) -> FlagHandler: ...

    @property
    def restart_handler(self) -> RestartHandler | None:
        """Rebuild + restart hook for the defense workflow.

        Default `None` is reserved for framework-internal test
        challenges. Externally contributed challenges must override.
        """
        return None

    @property
    @abstractmethod
    def docs(self) -> ChallengeDocs: ...
