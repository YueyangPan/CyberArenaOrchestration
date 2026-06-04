"""HealthChecker — runs a challenge's FunctionalityTest and emits one
SERVICE_HEALTH_CHECK event per CheckResult leaf.

Stage 4 (challenge interface v2): the framework no longer owns a 4-level
probe pattern. The challenge's own `FunctionalityTest.run(target)`
decides the shape of the result tree; HealthChecker just walks it and
records the leaves on the Timeline.

The recommended convention (HealthLevel.PROCESS/TCP/PROTOCOL/CHECKER) is
preserved by NotesChallenge and IcoChallenge so the existing panel and
SLA-window code keep seeing the same level names. Challenges with a
different shape (one leaf, deeper trees) work transparently — the panel
rendering for non-conventional names is a Stage 11 follow-up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from challenges.interface import Challenge, CheckResult, VulboxTarget
from gamemgr.events import EventType
from gamemgr.timeline import Timeline

log = logging.getLogger(__name__)

StatusResolver = Callable[[], dict]
"""() -> {'round': int, 'phase': str} for stamping events."""


class HealthChecker:
    def __init__(
        self,
        timeline: Timeline,
        *,
        status_resolver: StatusResolver,
    ) -> None:
        self.timeline = timeline
        self._status = status_resolver

    async def run(
        self,
        *,
        team_id: str,
        service: str,
        target: VulboxTarget,
        challenge: Challenge,
    ) -> CheckResult:
        """Run `challenge.functionality_test` against `target`, record one
        SERVICE_HEALTH_CHECK per leaf of the resulting CheckResult tree.

        Returns the root CheckResult so callers (poller, restart-post)
        can inspect overall pass/fail without re-walking the tree.
        """
        st = self._status()
        loop = asyncio.get_running_loop()
        # FunctionalityTest.run is blocking by contract (most probes use
        # docker exec under the hood). Run in the default executor so the
        # event loop stays responsive.
        result = await loop.run_in_executor(
            None, challenge.functionality_test.run, target,
        )

        for leaf in leaves(result):
            self.timeline.record(
                EventType.SERVICE_HEALTH_CHECK,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={
                    "team_id": team_id,
                    "service": service,
                    "level": leaf.name,
                    "passed": leaf.passed,
                    "detail": leaf.detail[-200:],
                },
            )
        return result


def leaves(node: CheckResult) -> list[CheckResult]:
    """Pre-order walk yielding only the leaves of the CheckResult tree.

    A leaf is a node with no children. Internal nodes (the root over a
    4-level chain) carry the aggregate pass/fail and are skipped — we
    emit one event per probed dimension, not for the rollup.
    """
    if not node.children:
        return [node]
    out: list[CheckResult] = []
    for child in node.children:
        out.extend(leaves(child))
    return out
