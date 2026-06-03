"""FlagServer — planting + rotation + submission verification.

Lifted out of `GameMgr` so flag mechanics are an independent facility
(parallel to `health.HealthPoller`). GameMgr keeps the public method
shapes (`plant_initial_flags`, `plant_flags`, `submit_flag`, `flags`)
but delegates the work here.

Dependencies are injected at construction:
  * `timeline`           — for FLAG_PLANTED / FLAG_SUBMITTED records
  * `topology`           — team_id → prod container name resolution
  * `service_by_owner`   — (team, service) → Service adapter for
                           SERVICE_PROTOCOL plants (mutable dict ref;
                           populated by GameMgr.deploy_challenges)
  * `spec_by_owner`      — (team, service) → ChallengeSpec for
                           FILE_AT_PATH plants (mutable dict ref)
  * `handle_resolver`    — container_name → ContainerHandle
  * `exec_in_container`  — docker exec for FILE_AT_PATH plant writes
  * `status_resolver`    — () → {round, phase} for FLAG_SUBMITTED stamping

Pluggable per Service ABC + FlagMechanism — no demo-service specifics
hardcoded.
"""

from __future__ import annotations

import logging
import shlex
from typing import Callable

from challenges.source import ChallengeSpec
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from orchestrator.service import ContainerHandle, Service

from .store import FlagStore, SubmitResult, new_flag

log = logging.getLogger(__name__)

ExecFn = Callable[[str, str], tuple[int, str]]
HandleResolver = Callable[[str], ContainerHandle]
StatusResolver = Callable[[], dict]


class FlagServer:
    def __init__(
        self,
        timeline: Timeline,
        *,
        topology,
        service_by_owner: dict[tuple[str, str], Service],
        spec_by_owner: dict[tuple[str, str], ChallengeSpec],
        handle_resolver: HandleResolver,
        exec_in_container: ExecFn,
        status_resolver: StatusResolver,
    ) -> None:
        self.timeline = timeline
        self.topology = topology
        self._service_by_owner = service_by_owner
        self._spec_by_owner = spec_by_owner
        self._handle = handle_resolver
        self._exec = exec_in_container
        self._status = status_resolver
        self.store = FlagStore()
        # Idempotency guard for rotate(): a re-entry on the same round
        # must not re-plant (defensive against any caller firing twice).
        self._last_planted_round: int = -1

    # ── planting ────────────────────────────────────────────────────────

    def plant_initial_flags(self) -> list[tuple[str, str, str]]:
        """Pre-game plant (round_id=0). Identical to plant_flags(0); kept
        under its own name so main.py and tests have an obvious entry point."""
        return self.plant_flags(round_id=0)

    def plant_flags(self, round_id: int) -> list[tuple[str, str, str]]:
        """Re-plant one fresh flag per (team, deployed service).

        Called once at pre-game (round_id=0) and at every DEFENSE entry
        (via `rotate`). `FlagStore.plant()` drops the prior flag from the
        inverted index so it can no longer be submitted — rotation
        invalidates stale flags immediately, by construction.

        Returns the list of (team_id, service_name, flag) planted.
        """
        planted: list[tuple[str, str, str]] = []
        team_to_prod = {t.id: t.prod for t in self.topology.teams}

        # SERVICE_PROTOCOL plants (notes-style: PUT via the service's
        # own protocol).
        for (team_id, service_name), service in self._service_by_owner.items():
            cname = team_to_prod[team_id]
            handle = self._handle(cname)
            flag = new_flag()
            log.info(
                "planting flag on %s/%s (round=%d, SERVICE_PROTOCOL)",
                team_id, service_name, round_id,
            )
            service.plant_flag(handle, flag)
            self.store.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))

        # FILE_AT_PATH plants (nautilus-ico-style: docker exec write).
        for (team_id, service_name), spec in self._spec_by_owner.items():
            cname = team_to_prod[team_id]
            flag = new_flag()
            flag_path = (spec.metadata or {}).get("flag_path", "/flag")
            log.info(
                "planting flag on %s/%s -> %s (round=%d, FILE_AT_PATH)",
                team_id, service_name, flag_path, round_id,
            )
            self._plant_file_at_path(cname, flag_path, flag)
            self.store.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))

        self._last_planted_round = round_id
        return planted

    def rotate(self, round_id: int) -> None:
        """Plant fresh flags for `round_id` and record FLAG_PLANTED per
        (team, service). Idempotent per round — a re-entry on the same
        round is a no-op (defensive). Called from GameMgr.enter_defense_phase.
        """
        if round_id == self._last_planted_round:
            return
        planted = self.plant_flags(round_id=round_id)
        for team_id, service_name, _flag in planted:
            self.timeline.record(
                EventType.FLAG_PLANTED,
                team_id=team_id,
                round_id=round_id,
                payload={"service": service_name},
            )

    def _plant_file_at_path(self, container_name: str, flag_path: str, flag: str) -> None:
        """Write `flag` (no trailing newline) to `flag_path` inside the
        container as root, mode 644. Raises if the write fails — a
        missing flag would silently break every CHECKER probe (e.g.
        ico reads /flag on CONNECT) and we want that surfaced loudly
        at deploy time.
        """
        # shlex.quote handles any quote/dollar/backtick in `flag` safely.
        cmd = (
            f"sh -c 'printf %s {shlex.quote(flag)} > {shlex.quote(flag_path)} "
            f"&& chmod 644 {shlex.quote(flag_path)}'"
        )
        rc, out = self._exec(container_name, cmd)
        if rc != 0:
            raise RuntimeError(
                f"FILE_AT_PATH plant failed on {container_name}:{flag_path}: "
                f"rc={rc}, out={out[-200:]!r}"
            )

    # ── submission ──────────────────────────────────────────────────────

    def submit(self, team_id: str, flag: str) -> SubmitResult:
        """Verify a submission against FlagStore and record FLAG_SUBMITTED.

        The flag string itself is NEVER recorded — the timeline is fanned
        out to the public panel WebSocket and persisted to JSONL; leaking
        flags there would give every observer an attack-for-free.
        Phase / round are stamped via the status_resolver so the panel
        can render which phase the submission happened in.
        """
        result = self.store.submit(team_id, flag)
        st = self._status()
        self.timeline.record(
            EventType.FLAG_SUBMITTED,
            team_id=team_id,
            round_id=st.get("round", 0),
            phase=st.get("phase", ""),
            payload={
                "accepted": result.accepted,
                "reason": result.reason,
                "submitter_team_id": team_id,
                "victim_team_id": result.victim_team_id,
                "service": result.service,
                "phase": st.get("phase", ""),
            },
        )
        return result

    # ── introspection ───────────────────────────────────────────────────

    def emit_planted_setup(self) -> None:
        """Record one FLAG_PLANTED per currently-planted (team, service)
        at round_id=0. Used by GameMgr.emit_setup_events for the pre-loop
        snapshot the panel reads when it boots."""
        for (team_id, service_name) in self.store.planted.keys():
            self.timeline.record(
                EventType.FLAG_PLANTED,
                team_id=team_id,
                round_id=0,
                payload={"service": service_name},
            )
