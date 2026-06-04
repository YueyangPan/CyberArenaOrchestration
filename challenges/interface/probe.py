"""Probes — anything that opens a connection to a running vulbox.

Functionality tests, flag handlers, and restart handlers are all probes.
They share a transport (HTTP, SSH, raw TCP, gRPC, docker exec, …) and
differ only in what they do once connected:

    FunctionalityTest — exercises the public contract and reports pass/fail
    FlagHandler       — plants and retrieves opaque flag strings
    RestartHandler    — rebuilds + restarts the service after defense edits

This module is transport-agnostic on purpose. A concrete probe can pull
in `requests`, `paramiko`, `httpx`, raw `socket`, whatever it needs.
The conventional path for handlers that need to exec inside the vulbox
container is to pull a callable out of `target.meta["exec_in_container"]`
— see the orchestrator's docs for the contract.

No imports from the rest of this codebase — stdlib only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class VulboxTarget:
    """Runtime coordinates of one deployed vulbox instance.

    Built by the orchestrator and handed to every probe call. A single
    `Challenge` is shared across all teams; per-team state lives here.

    Fields:
      host   — IP or hostname the probe can reach (e.g. the team's prod IP)
      ports  — named -> port number map. Probes pick the port by name so
               they don't bake in a specific number; lets the same probe
               work whether the orchestrator chose 8080 or 80.
               Example: {"http": 8080, "ssh": 22}
      meta   — open-ended dict for anything else a challenge needs at
               runtime: per-team credentials, signed tokens, a team_id, a
               tick number, and conventionally a callable
               `exec_in_container(container_name, cmd) -> (rc, output)`
               under key `"exec_in_container"` for handlers that need to
               run commands inside the vulbox. Probes that depend on a
               key should document it in their own docstring.
    """

    host: str
    ports: dict[str, int]
    meta: dict[str, Any] = field(default_factory=dict)


class HealthLevel(str, Enum):
    """Recommended naming convention for `FunctionalityTest.run()`'s
    CheckResult children.

    Concrete `FunctionalityTest`s that follow the DEFCON-style 4-level
    probe pattern (process → tcp → protocol → checker) should return a
    CheckResult tree whose direct children are named by these values.
    The orchestrator emits one `SERVICE_HEALTH_CHECK` event per leaf
    with `payload["level"] = child.name`, and the panel uses that name
    to render the per-level chip.

    Framework code does NOT branch on these values — a challenge whose
    health story doesn't fit the 4-level shape can return a different
    tree (one leaf, three leaves, nested groups). The enum is here
    purely as a shared vocabulary, not as a contract.
    """

    PROCESS = "process"      # service process is alive (pgrep-class check)
    TCP = "tcp"              # port accepts connections
    PROTOCOL = "protocol"    # speaks the protocol (real request → real response)
    CHECKER = "checker"      # gameplay-correct: vuln still reachable, flag retrievable


@dataclass
class CheckResult:
    """One node in the result tree returned by FunctionalityTest.run().

    A functionality test is rarely a single bool — a real app has multiple
    endpoints or flows, each pass/fail-able independently. We model the
    result as a tree so the poller can show "auth: OK, search: FAIL" rather
    than collapsing to a single bit.

    The top-level `passed` is the AND of all leaves (the test framework or
    the implementation decides; both are acceptable).
    """

    name: str
    passed: bool
    detail: str = ""
    children: list["CheckResult"] = field(default_factory=list)


class VulboxProbe(ABC):
    """Shared base for anything that talks to a deployed vulbox.

    A probe is independent of how many vulboxes exist — one probe instance
    is reused across teams and ticks. All per-call state arrives via the
    `VulboxTarget` argument on each subclass method.

    Subclasses declare which port name(s) they need so the orchestrator can
    validate wiring before the game starts.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, used in logs and the docs index."""
        ...

    @property
    def required_ports(self) -> tuple[str, ...]:
        """Names of ports this probe expects in VulboxTarget.ports.

        Default empty: probes that don't need any port (e.g. a probe that
        only reads files via a shared volume) can leave this alone.
        """
        return ()


class FunctionalityTest(VulboxProbe):
    """Probe a poller runs on a fixed cadence to confirm the vulbox works.

    Re-run every tick (or every N seconds). Must be idempotent and safe to
    run concurrently with the gameserver's flag traffic — it shares the
    same instance.

    Returning a `CheckResult` tree lets the poller report which subsystem
    broke. A team's SLA score is typically derived from the top-level
    `passed` over a window of ticks.
    """

    @abstractmethod
    def run(self, target: VulboxTarget) -> CheckResult:
        """Exercise the vulbox and return a pass/fail tree.

        Implementations should treat any exception they raise as an
        infrastructure failure, not a functional one — the orchestrator
        catches and logs them. A FAILED check that completed cleanly
        (the service was reachable but misbehaved) should return a
        `CheckResult(passed=False, …)`, not raise.
        """
        ...


class FlagHandler(VulboxProbe):
    """Probe that plants flags into a vulbox and reads them back.

    The gameserver issues a fresh flag string each tick. `plant` writes it
    into the vulbox (as a note, a database row, a file, an env var read by
    the app — the challenge decides). `retrieve` reads it back to verify
    either (a) the planter wrote correctly, or (b) an attacker successfully
    exfiltrated it from a target team's prod.

    The "handle" returned by `plant` is the locator the challenge uses to
    fetch the flag back — a note ID, a row PK, a URL path, etc. It is
    opaque to the orchestrator: pass it through unchanged.
    """

    @abstractmethod
    def plant(
        self,
        target: VulboxTarget,
        flag: str,
    ) -> str:
        """Write `flag` into the vulbox. Return an opaque handle.

        Called by the gameserver at the start of each tick with
        privileged/local access to the vulbox (e.g. via 127.0.0.1 inside
        the container or admin credentials). Should not depend on the
        cross-team attack network being open.
        """
        ...

    @abstractmethod
    def retrieve(
        self,
        target: VulboxTarget,
        handle: str,
    ) -> str | None:
        """Read the flag back. Return the flag string, or None if missing.

        Called in two contexts:
          - by the gameserver against its OWN vulbox to verify the plant
            took effect (uses privileged target);
          - by an attacking team's runner against a VICTIM vulbox over the
            cross-team network (uses whatever access the public interface
            allows — same code path, different target).

        The handle is the value returned from `plant` for this flag. If the
        flag has been wiped, rotated, or never planted, return None rather
        than raising.
        """
        ...


class RestartHandler(VulboxProbe):
    """Probe that rebuilds + restarts the service after defense edits.

    Defense-mode only. The orchestrator runs this under privileged access
    (root via docker exec) when the agent calls the MCP `restart_service`
    tool. Keeps the actual restart logic — the framework-owned scripts,
    the recompile/respawn pipeline, the user the service runs as — out
    of the agent's reach. Agents only TRIGGER restart through MCP; they
    never SSH in as the framework user to do it themselves.

    Challenges that don't have a defense workflow leave
    `Challenge.restart_handler = None`; the orchestrator's
    DEFENSE_KICKOFF dispatch and `restart_service` MCP tool both filter
    on this.

    `run()` returns a `CheckResult` summarising the restart: top-level
    `passed=True` when the service is back up and reachable; children
    may carry per-step detail (e.g. compile / spawn / smoke).
    """

    @abstractmethod
    def run(self, target: VulboxTarget) -> CheckResult:
        """Restart the service in-place. Block until the restart settles
        (or until an implementation-defined timeout) then return the
        outcome. Exceptions are treated as infrastructure failures by
        the orchestrator; a clean restart that ended in a broken service
        should return `CheckResult(passed=False, …)` not raise.
        """
        ...
