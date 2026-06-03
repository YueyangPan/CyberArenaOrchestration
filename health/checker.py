"""HealthChecker — 4-level probe primitive (PROCESS → TCP → PROTOCOL → CHECKER).

Lifted out of `GameMgr._emit_health_chain` to a standalone, injectable
class. Records one `SERVICE_HEALTH_CHECK` event per level on the
Timeline. Dependencies (docker exec, Service resolver, container handle
resolver, round/phase stamping) are passed via constructor so the
checker is unit-testable without a live game.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from challenges.source import ChallengeSpec, HealthLevel
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from orchestrator.service import ContainerHandle, Service

log = logging.getLogger(__name__)

ExecFn = Callable[[str, str], tuple[int, str]]
"""(container_name, cmd) -> (exit_code, output). Blocking docker exec."""

ServiceResolver = Callable[[str, str], "Service | None"]
"""(team_id, service_name) -> Service adapter for PROTOCOL probe, or None."""

HandleResolver = Callable[[str], ContainerHandle]
"""(container_name) -> ContainerHandle for Service.is_alive."""

StatusResolver = Callable[[], dict]
"""() -> {'round': int, 'phase': str} for stamping events."""


class HealthChecker:
    def __init__(
        self,
        timeline: Timeline,
        *,
        exec_in_container: ExecFn,
        service_resolver: ServiceResolver,
        handle_resolver: HandleResolver,
        status_resolver: StatusResolver,
    ) -> None:
        self.timeline = timeline
        self._exec = exec_in_container
        self._service = service_resolver
        self._handle = handle_resolver
        self._status = status_resolver

    async def run_chain(
        self,
        *,
        team_id: str,
        service: str,
        container_name: str,
        spec: ChallengeSpec,
    ) -> None:
        """Run PROCESS → TCP → PROTOCOL → CHECKER, record one event each.

        PROCESS: pgrep against spec.metadata.health_check.process_pattern
        TCP: orchestrator-side socket connect to spec.port
        PROTOCOL / CHECKER: defer to Service adapter if available;
          otherwise PROTOCOL passes with a reason and CHECKER aliases.
        """
        st = self._status()
        hc = (spec.metadata or {}).get("health_check") or {}
        loop = asyncio.get_running_loop()

        def _emit(level: HealthLevel, passed: bool, detail: str) -> None:
            self.timeline.record(
                EventType.SERVICE_HEALTH_CHECK,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={
                    "team_id": team_id,
                    "service": service,
                    "level": level.value,
                    "passed": passed,
                    "detail": detail,
                },
            )

        # PROCESS
        process_pattern = hc.get("process_pattern")
        if process_pattern:
            rc, _ = await loop.run_in_executor(
                None, self._exec, container_name,
                f"pgrep -f {process_pattern!r}",
            )
            _emit(HealthLevel.PROCESS, rc == 0, f"pgrep rc={rc}")
        else:
            _emit(HealthLevel.PROCESS, True, "no process_pattern; skipped")

        # TCP
        tcp_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {spec.port})); s.close()\""
        )
        rc, _ = await loop.run_in_executor(
            None, self._exec, container_name, tcp_probe,
        )
        _emit(HealthLevel.TCP, rc == 0, f"socket-connect rc={rc}")

        # PROTOCOL — defer to Service.is_alive(PROTOCOL) when available.
        # Without an adapter (e.g. nautilus-ico tested standalone), pass
        # with a reason; CHECKER below is the real gameplay-correctness signal.
        proto_ok = True
        service_obj = self._service(team_id, service)
        if service_obj is not None:
            handle = self._handle(container_name)
            try:
                proto_ok = await loop.run_in_executor(
                    None, lambda: service_obj.is_alive(handle, HealthLevel.PROTOCOL),
                )
                _emit(HealthLevel.PROTOCOL, bool(proto_ok), f"is_alive(PROTOCOL)={proto_ok}")
            except Exception as e:
                _emit(HealthLevel.PROTOCOL, False, f"is_alive PROTOCOL raised: {type(e).__name__}")
                proto_ok = False
        else:
            _emit(HealthLevel.PROTOCOL, True, "no service adapter; deferred to CHECKER")

        # CHECKER — runs the framework checker.sh when configured. notes
        # has no checker_command; aliases to PROTOCOL with a documented note.
        checker_cmd = hc.get("checker_command")
        if checker_cmd:
            rc, out = await loop.run_in_executor(
                None, self._exec, container_name, checker_cmd,
            )
            detail = (out.strip() or f"rc={rc}")[-200:]
            _emit(HealthLevel.CHECKER, rc == 0, detail)
        else:
            _emit(HealthLevel.CHECKER, bool(proto_ok), "no checker_command; aliased to PROTOCOL")
