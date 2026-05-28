"""Round state machine.

INIT -> PREFLIGHT -> SETUP -> DEFENSE <-> ATTACK -> STOPPED
                                  ↑          ↓
                                  └── round+=1
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from .config import RoundConfig
from .events import EventBus, EventType, new_event

log = logging.getLogger(__name__)


class Phase(str, Enum):
    INIT = "INIT"
    PREFLIGHT = "PREFLIGHT"
    SETUP = "SETUP"
    DEFENSE = "DEFENSE"
    ATTACK = "ATTACK"
    STOPPED = "STOPPED"


class RoundLoop:
    def __init__(self, bus: EventBus, network, config: RoundConfig) -> None:
        self.bus = bus
        self.network = network        # orchestrator.infra.network.NetworkManager
        self.config = config
        self.phase: Phase = Phase.INIT
        self.round: int = 0
        self._stop_event = asyncio.Event()
        # Phase-scoped early-end signal — set by GameMgr when all teams
        # have submitted DEPLOY_COMPLETE for the current DEFENSE phase
        # (later: same for ATTACK if symmetric early-end is wanted).
        # Reset at the start of every phase so the signal applies to one
        # phase only.
        self._phase_early_end_event = asyncio.Event()

    async def run(self) -> None:
        log.info(
            "round loop start: total_rounds=%d defense=%ds attack=%ds",
            self.config.total_rounds, self.config.defense_secs, self.config.attack_secs,
        )
        # Reason tracking for GAME_END payload. "completed" = ran the full
        # total_rounds; "stopped" = stop() was called mid-loop; "error" =
        # an unhandled exception bubbled out of the body.
        reason = "completed"
        try:
            while not self._stop_event.is_set() and self.round < self.config.total_rounds:
                self.round += 1
                await self.bus.publish(
                    new_event(EventType.ROUND_START, round_id=self.round, phase=self.phase.value)
                )

                await self._enter_phase(Phase.DEFENSE, allow_cross_team=False, duration=self.config.defense_secs)
                if self._stop_event.is_set():
                    reason = "stopped"
                    break

                await self._enter_phase(Phase.ATTACK, allow_cross_team=True, duration=self.config.attack_secs)

                await self.bus.publish(
                    new_event(EventType.ROUND_STOP, round_id=self.round, phase=self.phase.value)
                )
            else:
                # while-loop fell through because total_rounds reached or
                # stop_event was set at the top of an iteration.
                if self._stop_event.is_set():
                    reason = "stopped"
        except Exception:
            reason = "error"
            raise
        finally:
            final_round = self.round
            final_phase = self.phase.value
            self.phase = Phase.STOPPED
            log.info(
                "round loop stopped at round=%d phase=%s reason=%s",
                final_round, final_phase, reason,
            )
            # GAME_END fires from inside `finally` so it lands even on
            # exception. Best-effort: if the bus is already torn down or
            # the publish raises, swallow it — the game is exiting anyway.
            try:
                await self.bus.publish(
                    new_event(
                        EventType.GAME_END,
                        round_id=final_round,
                        phase=Phase.STOPPED.value,
                        payload={
                            "reason": reason,
                            "final_round": final_round,
                            "exited_from_phase": final_phase,
                            "total_rounds": self.config.total_rounds,
                        },
                    )
                )
            except Exception:
                log.exception("publishing GAME_END failed; loop already exiting")

    async def _enter_phase(self, phase: Phase, *, allow_cross_team: bool, duration: int) -> None:
        self.phase = phase
        # Reset the per-phase early-end signal so a previous phase's end
        # doesn't immediately collapse this one.
        self._phase_early_end_event.clear()
        self.network.set_cross_team_network(allow_cross_team)
        await self.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=self.round, phase=phase.value)
        )
        await self._wait(duration)

    async def _wait(self, secs: int) -> None:
        """Wait up to `secs` seconds, but wake early if either:
          - `_stop_event` fires (game shutdown)
          - `_phase_early_end_event` fires (e.g. all teams done with DEFENSE)
        """
        stop_task = asyncio.create_task(self._stop_event.wait())
        early_task = asyncio.create_task(self._phase_early_end_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, early_task},
                timeout=secs,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, early_task):
                if not t.done():
                    t.cancel()

    def end_phase_early(self) -> None:
        """Cut the current phase's wait short. GameMgr calls this when
        every team has reported DEPLOY_COMPLETE for the current DEFENSE
        round, so the game can move on to ATTACK without burning the
        rest of the timer.
        """
        self._phase_early_end_event.set()

    def stop(self) -> None:
        self._stop_event.set()