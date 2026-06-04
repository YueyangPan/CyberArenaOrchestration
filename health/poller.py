"""HealthPoller — periodic + on-demand health-check scheduler.

The poller does NOT subscribe to anything. GameMgr drives it at phase
boundaries:

  enter_attack_phase  → poller.on_attack_enter(round_id)
    fires one immediate probe per target (replaces the legacy
    end-of-defense health check) and starts a per-(team, service)
    task that loops `await asyncio.sleep(interval); checker.run()`.

  enter_defense_phase / GAME_END / stop → poller.on_attack_exit()
    cancels all periodic tasks.

  _run_restart_job (any phase) → poller.trigger_now(team, service)
    one-shot chain; agent gets a SERVICE_HEALTH_CHECK on the timeline
    shortly after the restart_cmd settles.

Stage 4 (challenge interface v2): PollTarget carries a `Challenge`
instance and a pre-built `VulboxTarget`; HealthChecker.run delegates
to `challenge.functionality_test.run(target)`. Interval is read from
the v1 ChallengeSpec metadata as a transitional bridge until Stage 11
defines the post-v2 interval source.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from challenges.interface import Challenge, VulboxTarget

from .checker import HealthChecker

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECS = 10
"""Fallback when a defense-capable challenge has no per-spec interval."""


@dataclass(frozen=True)
class PollTarget:
    team_id: str
    service: str
    challenge: Challenge
    target: VulboxTarget
    interval_secs: float


class HealthPoller:
    def __init__(self, checker: HealthChecker, targets: list[PollTarget]) -> None:
        self.checker = checker
        self.targets = targets
        # Per-(team, service) periodic task; populated on on_attack_enter,
        # cancelled on on_attack_exit / stop.
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._running = False

    def on_attack_enter(self, round_id: int) -> list[asyncio.Task]:
        """Start polling: fire one immediate probe per target (as a
        fire-and-forget Task so the caller isn't blocked), then start a
        periodic task per target. Returns the immediate-probe tasks so
        tests can await them; production callers ignore.
        """
        if self._running:
            log.warning("poller.on_attack_enter while already running; restarting")
            self.on_attack_exit()
        self._running = True

        immediate: list[asyncio.Task] = []
        for t in self.targets:
            immediate.append(asyncio.create_task(self._safe_run(t)))
            self._tasks[(t.team_id, t.service)] = asyncio.create_task(
                self._periodic_loop(t)
            )
        return immediate

    def on_attack_exit(self) -> None:
        """Cancel all periodic tasks. Idempotent."""
        self._running = False
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    async def trigger_now(self, team_id: str, service: str) -> None:
        """One-shot probe for a (team, service) — used by restart-post.
        Looks up the matching PollTarget; no-op if the service isn't
        defense-capable (no target was registered)."""
        target = next(
            (t for t in self.targets if t.team_id == team_id and t.service == service),
            None,
        )
        if target is None:
            log.debug(
                "trigger_now: no poll target for (%s, %s); skipping",
                team_id, service,
            )
            return
        await self._safe_run(target)

    async def stop(self) -> None:
        """Cancel everything and wait for tasks to settle. Safe to call
        multiple times."""
        self.on_attack_exit()

    async def _periodic_loop(self, target: PollTarget) -> None:
        try:
            while True:
                await asyncio.sleep(target.interval_secs)
                await self._safe_run(target)
        except asyncio.CancelledError:
            return

    async def _safe_run(self, target: PollTarget) -> None:
        """Run one chain; log exceptions instead of propagating so a
        single bad probe doesn't kill the periodic task."""
        try:
            await self.checker.run(
                team_id=target.team_id,
                service=target.service,
                target=target.target,
                challenge=target.challenge,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "health probe raised for (%s, %s); continuing",
                target.team_id, target.service,
            )


def build_targets(
    *,
    challenges,
    teams_by_id,
    challenge_by_name,
    target_factory,
    resolve_spec,
) -> list[PollTarget]:
    """Build the poll-target list from game config.

    Cross-product (config.challenges × deploy_on teams) filtered to
    defense-capable challenges (those whose Challenge.restart_handler
    is not None). Interval still comes from the v1 ChallengeSpec
    metadata (`health_check.interval_secs`) as a transitional bridge.

    `challenges`        — iterable of ChallengeConfig.
    `teams_by_id`       — dict of team_id -> Team (must expose `.prod`).
    `challenge_by_name` — callable: chcfg.name -> Challenge instance.
    `target_factory`    — callable: (team_id, challenge) -> VulboxTarget;
                          typically `GameMgr.target_for`.
    `resolve_spec`      — callable: chcfg.name -> ChallengeSpec | None;
                          used only to read the interval_secs override.
    """
    targets: list[PollTarget] = []
    for chcfg in challenges:
        challenge = challenge_by_name(chcfg.name)
        # Only poll defense-capable challenges. Attack-only challenges
        # (e.g. notes) have restart_handler=None — periodic CHECKER
        # probes there would be wasted load.
        if challenge.restart_handler is None:
            continue

        spec = resolve_spec(chcfg.name)
        hc = ((spec.metadata if spec is not None else None) or {}).get("health_check") or {}
        interval = hc.get("interval_secs")
        if interval is None:
            log.warning(
                "challenge %r has no health_check.interval_secs; defaulting to %ds",
                chcfg.name, DEFAULT_INTERVAL_SECS,
            )
            interval = DEFAULT_INTERVAL_SECS

        for team_id in chcfg.deploy_on:
            team = teams_by_id.get(team_id)
            if team is None or not team.prod:
                continue
            target = target_factory(team_id, challenge)
            targets.append(PollTarget(
                team_id=team_id,
                service=chcfg.name,
                challenge=challenge,
                target=target,
                interval_secs=float(interval),
            ))
    return targets
