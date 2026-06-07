"""Probes — anything that opens a connection to a running vulbox.

  FunctionalityTest — pass/fail probe of the public contract
  FlagHandler       — plant + retrieve flags
  RestartHandler    — rebuild + restart after a defense edit

Transport-agnostic. Handlers that need to run commands inside the
container pull a `(host, cmd) -> (rc, output)` callable out of
`target.meta["exec_in_container"]`.

Stdlib-only — no imports from the rest of the codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class VulboxTarget:
    """Runtime coordinates of one deployed vulbox.

    Built per-call by the orchestrator and handed to every probe.

    Fields:
      host  — IP or hostname the probe can reach
      ports — named -> port-number map. Probes select by name so the
              same probe code works at any host port.
      meta  — open-ended dict. By convention includes:
                exec_in_container(container_name, cmd) -> (rc, output)
              for handlers that need to run commands inside the vulbox.
    """

    host: str
    ports: dict[str, int]
    meta: dict[str, Any] = field(default_factory=dict)


class HealthLevel(str, Enum):
    """Recommended names for the four standard `FunctionalityTest`
    children. Convention, not a contract — the framework doesn't branch
    on these. Use them so the panel renders the familiar 4-chip strip.
    """

    PROCESS = "process"
    TCP = "tcp"
    PROTOCOL = "protocol"
    CHECKER = "checker"


@dataclass
class CheckResult:
    """One node in the tree `FunctionalityTest.run()` / `RestartHandler.run()` return.

    Top-level `passed` is the AND of all leaves.
    """

    name: str
    passed: bool
    detail: str = ""
    children: list["CheckResult"] = field(default_factory=list)


class VulboxProbe(ABC):
    """Base for anything that talks to a deployed vulbox.

    One probe instance is reused across teams and ticks — per-call state
    arrives via the `VulboxTarget` argument, not on `self`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for logs + dashboards."""
        ...

    @property
    def required_ports(self) -> tuple[str, ...]:
        """Port names this probe expects in `VulboxTarget.ports`."""
        return ()


class FunctionalityTest(VulboxProbe):
    """Periodic / restart-post probe that confirms the vulbox still works."""

    @abstractmethod
    def run(self, target: VulboxTarget) -> CheckResult:
        """Exercise the vulbox and return a pass/fail tree.

        Implementation failures (the service unreachable, broken
        gameplay) should land as `CheckResult(passed=False, ...)`.
        Raise only for infrastructure-level errors.
        """
        ...


class FlagHandler(VulboxProbe):
    """Plants flags into a vulbox and reads them back.

    `plant` returns an opaque handle — `retrieve` uses it to locate the
    flag later (a path, row id, URL, whatever fits the challenge).
    """

    @abstractmethod
    def plant(self, target: VulboxTarget, flag: str) -> str:
        """Write `flag` to the vulbox and return the locator. Called by
        the gameserver with privileged access."""
        ...

    @abstractmethod
    def retrieve(self, target: VulboxTarget, handle: str) -> str | None:
        """Read the planted flag. Returns None when missing — never raises."""
        ...


class RestartHandler(VulboxProbe):
    """Rebuilds + restarts the service after a defense edit.

    Invoked under privileged docker exec when the agent calls
    `restart_service` via MCP. Agents trigger this through the MCP tool
    only — they cannot run the underlying script themselves.

    Return a `CheckResult` describing the outcome (root `passed=True`
    when the service is healthy after restart). Children can carry
    per-step detail (compile / spawn / smoke).
    """

    @abstractmethod
    def run(self, target: VulboxTarget) -> CheckResult: ...
