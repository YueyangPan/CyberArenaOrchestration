"""FlagServer — planting + rotation + submission verification.

Independent facility consumed by GameMgr. Plant goes through
`Challenge.flag_handler.plant(target, flag)`; FlagServer doesn't know
the per-challenge mechanism.

Dependencies:
  ctx                — FacilityContext: `ctx.events` receives
                       FLAG_PLANTED / FLAG_SUBMITTED records, `ctx.status`
                       supplies round/phase for stamping + phase gating
  challenge_by_owner — (team, service) -> Challenge; mutable dict
                       reference, populated by GameMgr.deploy_challenges
  target_factory     — (team_id, challenge) -> VulboxTarget; typically
                       GameMgr.target_for, so FlagServer doesn't need
                       to know about topology. The target's exec callable
                       is itself backed by `ctx.exec`.
"""

from __future__ import annotations

import logging
from typing import Callable

from challenges.interface import Challenge, VulboxTarget
from facility import Facility, FacilityContext
from gamemgr.events import EventType

from .store import FlagStore, SubmitResult, new_flag

log = logging.getLogger(__name__)

TargetFactory = Callable[[str, Challenge], VulboxTarget]


class FlagServer(Facility):
    def __init__(
        self,
        ctx: FacilityContext,
        *,
        challenge_by_owner: dict[tuple[str, str], Challenge],
        target_factory: TargetFactory,
    ) -> None:
        self._events = ctx.events
        self._status = ctx.status
        self._challenge_by_owner = challenge_by_owner
        self._target = target_factory
        self.store = FlagStore()
        # Handle returned by the most recent plant per (team, service).
        # Reserved for future retrieve() paths (own-team verify, attack
        # agents using the same handler); no caller reads it today.
        self._handles: dict[tuple[str, str], str] = {}
        # Idempotency guard for rotate(): a re-entry on the same round
        # must not re-plant (defensive against any caller firing twice).
        self._last_planted_round: int = -1

    @property
    def name(self) -> str:
        return "flag"

    # ── planting ────────────────────────────────────────────────────────

    def plant_initial_flags(self) -> list[tuple[str, str, str]]:
        """Pre-game plant. Alias for `plant_flags(0)` so main.py + tests
        have a clearly-named entry point."""
        return self.plant_flags(round_id=0)

    def plant_flags(self, round_id: int) -> list[tuple[str, str, str]]:
        """Re-plant one fresh flag per (team, deployed challenge).

        `FlagStore.plant()` drops the prior flag from the inverted index,
        so rotation invalidates stale flags immediately.

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
        """Plant fresh flags and record FLAG_PLANTED per (team, service).
        Idempotent per round. Called from GameMgr.enter_defense_phase."""
        if round_id == self._last_planted_round:
            return
        planted = self.plant_flags(round_id=round_id)
        for team_id, service_name, _flag in planted:
            self._events.record(
                EventType.FLAG_PLANTED,
                team_id=team_id,
                round_id=round_id,
                payload={"service": service_name},
            )

    # ── submission ──────────────────────────────────────────────────────

    def submit(self, team_id: str, flag: str) -> SubmitResult:
        """Verify a submission and record FLAG_SUBMITTED.

        The flag string is never written to the timeline — observers
        consume the timeline and we don't want flags leaking there.

        DEFENSE-phase submissions return WRONG_PHASE *before* the
        FlagStore is consulted, so a confused defense agent doesn't
        burn its DUPLICATE slot for the next ATTACK retry.
        """
        st = self._status.status()
        phase = (st.get("phase") or "").upper()
        if phase == "DEFENSE":
            result = SubmitResult(accepted=False, reason="WRONG_PHASE")
        else:
            result = self.store.submit(team_id, flag)
        self._events.record(
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
            self._events.record(
                EventType.FLAG_PLANTED,
                team_id=team_id,
                round_id=0,
                payload={"service": service_name},
            )
