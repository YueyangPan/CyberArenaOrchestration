"""HealthChecker — run a challenge's FunctionalityTest, emit one
SERVICE_HEALTH_CHECK event per CheckResult leaf.

The challenge's `FunctionalityTest.run(target)` decides the shape of
the result tree; this module just walks it and records leaves. The
bundled challenges follow the HealthLevel convention
(process/tcp/protocol/checker) so the panel renders the familiar
4-chip strip; non-conventional tree shapes work too, the panel just
shows the raw leaf names.
"""

from __future__ import annotations

import asyncio
import logging

from challenges.interface import Challenge, CheckResult, VulboxTarget
from facility import FacilityContext
from gamemgr.events import EventType

log = logging.getLogger(__name__)


class HealthChecker:
    """Stateless probe runner. Uses `ctx.events` to record results and
    `ctx.status` to stamp round/phase; never touches `ctx.exec` directly
    (the exec callable rides inside the `VulboxTarget` it's handed)."""

    def __init__(self, ctx: FacilityContext) -> None:
        self._events = ctx.events
        self._status = ctx.status

    async def run(
        self,
        *,
        team_id: str,
        service: str,
        target: VulboxTarget,
        challenge: Challenge,
        trigger: str = "periodic",
    ) -> CheckResult:
        """Run the functionality test and emit one SERVICE_HEALTH_CHECK
        per leaf. Returns the root CheckResult.

        `trigger` records WHY this probe ran — "periodic" (the poll loop),
        "attack_enter" (the one-shot at ATTACK entry), or "restart" (the
        post-restart probe). The periodic cadence and these one-shots
        interleave on the same lane, so the tag is what lets a reader tell
        why two checks are closer together than the poll interval.
        """
        st = self._status.status()
        loop = asyncio.get_running_loop()
        # functionality_test.run is blocking (docker exec under the hood),
        # so it goes through the default executor.
        result = await loop.run_in_executor(
            None, challenge.functionality_test.run, target,
        )

        for leaf in leaves(result):
            self._events.record(
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
                    "trigger": trigger,
                },
            )
        return result


def leaves(node: CheckResult) -> list[CheckResult]:
    """Pre-order walk yielding only nodes with no children. Internal
    nodes (the root rollup over a 4-level chain) are skipped — we
    emit one event per probed dimension, not for the aggregate."""
    if not node.children:
        return [node]
    out: list[CheckResult] = []
    for child in node.children:
        out.extend(leaves(child))
    return out
