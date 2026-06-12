"""Async-mode game driver.

Parallel to `RoundLoop`, selected when `round_config.mode == "async"`.

Where `RoundLoop` runs a strict DEFENSE↔ATTACK state machine (toggling the
cross-team network each phase and spawning a fresh agent per phase), `AsyncLoop`
runs ONE continuous game at `Phase.LIVE`:

  - cross-team networks are connected ONCE at start and stay up the whole game
    (attack + defense happen simultaneously — no protected patch window),
  - one long-lived supervised agent per team is started once (the supervisor
    lives in main.py; AsyncLoop just kicks it via `game.start_async_agents()`),
  - health polling runs continuously (`on_attack_enter` once, never exited until
    game end),
  - a "round" tick exists only to rotate flags every `round_secs` and bound
    scoring; flag submission is accepted at all times (the WRONG_PHASE gate keys
    on DEFENSE, which LIVE is not).

AsyncLoop deliberately does NOT call `enter_defense_phase` / `enter_attack_phase`
— those carry the sync per-phase kickoff + network toggle semantics.

It mirrors the small surface `GameMgr.status()` / `stop()` read off the active
driver (`.phase`, `.round`, `.stop()`, `.end_phase_early()`), so GameMgr can hold
either loop in `self.round_loop` ("the active driver") without branching.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import RoundConfig
from .round import Phase

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .game_mgr import GameMgr

log = logging.getLogger(__name__)


class AsyncLoop:
    def __init__(self, game: "GameMgr", network, config: RoundConfig) -> None:
        self.game = game
        self.network = network        # orchestrator.infra.network.NetworkManager
        self.config = config
        self.phase: Phase = Phase.INIT
        self.round: int = 0
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        log.info(
            "async loop start: total_rounds=%d round_secs=%ds (networks always-on)",
            self.config.total_rounds, self.config.round_secs,
        )
        reason = "completed"
        try:
            # Networks ON for the whole game — attack and defense happen
            # simultaneously. Toggled once here, never flipped per round.
            self.network.set_cross_team_network(True)
            self.phase = Phase.LIVE

            # Continuous health polling for the whole game (one immediate
            # probe + periodic loop per target). Never on_attack_exit until
            # teardown — that's what makes it continuous.
            if self.game.health_poller is not None:
                self.game.health_poller.on_attack_enter(0)

            # One long-lived supervised agent per team, prompted once. The
            # supervisor body lives in main.py; this just fires it.
            self.game.start_async_agents()

            # Round tick = flag-rotation interval. Scoring/health run the
            # whole time; the only per-round side effect is rotate + a
            # periodic service-log snapshot.
            while not self._stop_event.is_set() and self.round < self.config.total_rounds:
                self.round += 1
                self.game._async_round_started_ts = time.time()
                self.game.on_round_start(self.round, Phase.LIVE.value)
                self.game.flag_server.rotate(self.round)
                await self._wait(self.config.round_secs)
                if self.game.artifacts is not None:
                    await self.game._persist_service_logs(self.round)
            else:
                if self._stop_event.is_set():
                    reason = "stopped"
        except Exception:
            reason = "error"
            raise
        finally:
            final_round = self.round
            self.phase = Phase.STOPPED
            # Release any supervisor blocked on is_game_over() so it stops
            # respawning the agent.
            self.game._game_over.set()
            log.info(
                "async loop stopped at round=%d reason=%s", final_round, reason,
            )
            try:
                self.game.on_game_end(
                    final_round,
                    Phase.STOPPED.value,
                    {
                        "reason": reason,
                        "final_round": final_round,
                        "exited_from_phase": Phase.LIVE.value,
                        "total_rounds": self.config.total_rounds,
                        "mode": "async",
                    },
                )
            except Exception:
                log.exception("on_game_end failed; loop already exiting")

    async def _wait(self, secs: int) -> None:
        """Wait up to `secs`, waking early if `_stop_event` fires (shutdown).

        Mirrors RoundLoop._wait minus the phase-early-end signal (async has
        no per-phase early end — rounds are fixed flag-rotation intervals).
        """
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait({stop_task}, timeout=secs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not stop_task.done():
                stop_task.cancel()

    def end_phase_early(self) -> None:
        """No-op: async has no per-phase early end. Present so GameMgr's
        late-DEPLOY_COMPLETE path (guarded to DEFENSE, never reached at LIVE)
        can call it on the active driver without an AttributeError."""

    def stop(self) -> None:
        self._stop_event.set()
        # Mirror RoundLoop.stop()'s effect on the rest of the system: trip
        # the game-over gate so the agent supervisors stop respawning even
        # before the loop's finally block runs.
        self.game._game_over.set()
