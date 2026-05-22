"""GameMgr — deploys challenges and drives the round loop.

Lifecycle:
  deploy_challenges()   -> sync: install + start every challenge on its target containers
  start()               -> async: wires file sink + runs the round loop until done/stop
  stop()                -> async: signals the round loop and closes the file sink
"""

from __future__ import annotations

import logging
from pathlib import Path

from orchestrator.orchestrator import Orchestrator
from orchestrator.service import ContainerHandle, Service

from challenges.registry import build as build_challenge

from .config import ChallengeConfig, GameConfig
from .events import EventBus, EventType, FileSink, new_event, new_run_dir
from .round import Phase, RoundLoop

log = logging.getLogger(__name__)


class GameMgr:
    def __init__(self, config: GameConfig, orchestrator: Orchestrator) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.bus = EventBus()
        self.round_loop: RoundLoop | None = None
        self.run_dir: Path | None = None
        self._file_sink: FileSink | None = None
        self._deployed: list[tuple[Service, list[ContainerHandle]]] = []

    # ── lifecycle ───────────────────────────────────────────────────────

    def deploy_challenges(self) -> None:
        """Build every challenge from the registry, install + start on each
        target container.  Runs synchronously before the round loop starts."""

        team_to_prod = {t.id: t.prod for t in self.config.topology.teams}

        for chcfg in self.config.challenges:
            service = build_challenge(chcfg.type, port=chcfg.port)
            handles: list[ContainerHandle] = []
            for team_id in chcfg.deploy_on:
                cname = team_to_prod.get(team_id)
                if cname is None:
                    raise KeyError(f"challenge {chcfg.name!r}: unknown team_id {team_id!r}")
                handle = self._handle_for(cname)
                log.info("deploying challenge %s -> %s (port %d)", chcfg.name, cname, chcfg.port)
                service.install(handle)
                service.start(handle)
                handles.append(handle)
            self._deployed.append((service, handles))

    def teardown_challenges(self) -> None:
        for service, handles in self._deployed:
            for h in handles:
                try:
                    service.stop(h)
                except Exception:
                    log.exception("stop failed for %s on %s", service.name, h.name)
        self._deployed.clear()

    # ── round loop ──────────────────────────────────────────────────────

    async def start(self, run_dir: Path | None = None) -> None:
        self.run_dir = run_dir or new_run_dir()
        self._file_sink = FileSink(self.run_dir / "events.jsonl")
        self.bus.subscribe(self._file_sink)
        log.info("game start; events -> %s", self._file_sink.path)

        # Emit one deploy event per challenge so the log captures setup
        for service, handles in self._deployed:
            for h in handles:
                await self.bus.publish(
                    new_event(
                        EventType.SERVICE_DEPLOYED,
                        payload={"service": service.name, "container": h.name, "port": service.port},
                    )
                )

        self.round_loop = RoundLoop(self.bus, self.orchestrator.network, self.config.rounds)
        await self.round_loop.run()

    async def stop(self) -> None:
        if self.round_loop is not None:
            self.round_loop.stop()
        if self._file_sink is not None:
            self._file_sink.close()
            self._file_sink = None

    # ── read-only views ─────────────────────────────────────────────────

    def status(self) -> dict:
        if self.round_loop is None:
            return {"phase": Phase.INIT.value, "round": 0, "total_rounds": self.config.rounds.total_rounds}
        return {
            "phase": self.round_loop.phase.value,
            "round": self.round_loop.round,
            "total_rounds": self.config.rounds.total_rounds,
        }

    def submit_flag(self, team_id: str, flag: str) -> bool:
        # M5 wires this up.
        return False

    # ── helpers ─────────────────────────────────────────────────────────

    def _handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))