"""FlagServer — planting + rotation + submission verification.

Lifted out of `GameMgr` so flag mechanics are an independent facility
(parallel to `health.HealthPoller`). GameMgr keeps the public method
shapes (`plant_initial_flags`, `plant_flags`, `submit_flag`, `flags`)
but delegates the work here.

Stage 3 (challenge interface v2): the per-mechanism branching is gone.
FlagServer no longer knows about SERVICE_PROTOCOL vs FILE_AT_PATH —
plant goes through `Challenge.flag_handler.plant(target, flag)` and the
challenge owns its transport. The (team, service) -> Challenge map is
populated by GameMgr.deploy_challenges and passed in by reference.

Dependencies are injected at construction:
  * `timeline`           — for FLAG_PLANTED / FLAG_SUBMITTED records
  * `challenge_by_owner` — (team, service) -> Challenge instance
                           (mutable dict ref; populated by
                           GameMgr.deploy_challenges)
  * `target_factory`     — (team_id, challenge) -> VulboxTarget; built
                           by GameMgr (`GameMgr.target_for`) so flag
                           handlers see the privileged docker-exec
                           channel without FlagServer touching topology
  * `status_resolver`    — () -> {round, phase} for FLAG_SUBMITTED stamping
"""

from __future__ import annotations

import logging
from typing import Callable

from challenges.interface import Challenge, VulboxTarget
from gamemgr.events import EventType
from gamemgr.timeline import Timeline

from .store import FlagStore, SubmitResult, new_flag

log = logging.getLogger(__name__)

TargetFactory = Callable[[str, Challenge], VulboxTarget]
StatusResolver = Callable[[], dict]


class FlagServer:
    def __init__(
        self,
        timeline: Timeline,
        *,
        challenge_by_owner: dict[tuple[str, str], Challenge],
        target_factory: TargetFactory,
        status_resolver: StatusResolver,
    ) -> None:
        self.timeline = timeline
        self._challenge_by_owner = challenge_by_owner
        self._target = target_factory
        self._status = status_resolver
        self.store = FlagStore()
        # Per-(team, service) handle returned by the most recent plant.
        # Kept so future retrieve() paths (own-team verify, attack-agent
        # retrieve over the same handler) can locate the planted flag.
        # No caller reads this yet in Stage 3.
        self._handles: dict[tuple[str, str], str] = {}
        # Idempotency guard for rotate(): a re-entry on the same round
        # must not re-plant (defensive against any caller firing twice).
        self._last_planted_round: int = -1

    # ── planting ────────────────────────────────────────────────────────

    def plant_initial_flags(self) -> list[tuple[str, str, str]]:
        """Pre-game plant (round_id=0). Identical to plant_flags(0); kept
        under its own name so main.py and tests have an obvious entry point."""
        return self.plant_flags(round_id=0)

    def plant_flags(self, round_id: int) -> list[tuple[str, str, str]]:
        """Re-plant one fresh flag per (team, deployed challenge).

        Called once at pre-game (round_id=0) and at every DEFENSE entry
        (via `rotate`). `FlagStore.plant()` drops the prior flag from the
        inverted index so it can no longer be submitted — rotation
        invalidates stale flags immediately, by construction.

        Returns the list of (team_id, service_name, flag) planted.
        """
        planted: list[tuple[str, str, str]] = []
        for (team_id, service_name), challenge in self._challenge_by_owner.items():
            target = self._target(team_id, challenge)
            flag = new_flag()
            log.info(
                "planting flag on %s/%s (round=%d, handler=%s)",
                team_id, service_name, round_id, challenge.flag_handler.name,
            )
            handle = challenge.flag_handler.plant(target, flag)
            self._handles[(team_id, service_name)] = handle
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
