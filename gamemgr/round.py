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

    async def run(self) -> None:
        log.info(
            "round loop start: total_rounds=%d defense=%ds attack=%ds",
            self.config.total_rounds, self.config.defense_secs, self.config.attack_secs,
        )
        try:
            while not self._stop_event.is_set() and self.round < self.config.total_rounds:
                self.round += 1
                await self.bus.publish(
                    new_event(EventType.ROUND_START, round_id=self.round, phase=self.phase.value)
                )

                await self._enter_phase(Phase.DEFENSE, allow_cross_team=False, duration=self.config.defense_secs)
                if self._stop_event.is_set():
                    break

                await self._enter_phase(Phase.ATTACK, allow_cross_team=True, duration=self.config.attack_secs)

                await self.bus.publish(
                    new_event(EventType.ROUND_STOP, round_id=self.round, phase=self.phase.value)
                )
        finally:
            self.phase = Phase.STOPPED
            log.info("round loop stopped at round=%d phase=%s", self.round, self.phase.value)

    async def _enter_phase(self, phase: Phase, *, allow_cross_team: bool, duration: int) -> None:
        self.phase = phase
        try:
            self.network.set_cross_team_network(allow_cross_team)
        except NotImplementedError:
            pass  # M2: network switching is a stub
        await self.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=self.round, phase=phase.value)
        )
        await self._wait(duration)

    async def _wait(self, secs: int) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop_event.set()