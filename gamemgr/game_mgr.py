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
from .flag import FlagStore, SubmitResult, new_flag
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
        self.flags = FlagStore()
        # (team_id, service_name) -> Service instance, populated by deploy_challenges
        # so plant_initial_flags can find the right adapter per (team, service).
        self._service_by_owner: dict[tuple[str, str], Service] = {}

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
                self._service_by_owner[(team_id, service.name)] = service
            self._deployed.append((service, handles))

    def plant_initial_flags(self) -> list[tuple[str, str, str]]:
        """Generate and plant one flag per (team, deployed service).

        Synchronous — runs after deploy_challenges() and before the round
        loop starts. Returns the list of (team_id, service_name, flag) that
        were planted (handy for tests; main.py ignores the return value).

        Per design_v1.md §3.1 this is a one-shot at game start; per-round
        rotation is deferred to M5.5.
        """
        planted: list[tuple[str, str, str]] = []
        team_to_prod = {t.id: t.prod for t in self.config.topology.teams}

        for (team_id, service_name), service in self._service_by_owner.items():
            cname = team_to_prod[team_id]
            handle = self._handle_for(cname)
            flag = new_flag()
            log.info("planting flag on %s/%s", team_id, service_name)
            service.plant_flag(handle, flag)
            self.flags.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))
        return planted

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

        # One event per planted (team, service). Flag string deliberately omitted
        # — the bus is broadcast to the public panel.
        for (team_id, service_name) in self.flags.planted.keys():
            await self.bus.publish(
                new_event(
                    EventType.FLAG_PLANTED,
                    team_id=team_id,
                    payload={"service": service_name},
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

    async def submit_flag(self, team_id: str, flag: str) -> SubmitResult:
        """Verify a flag submission and emit FLAG_SUBMITTED.

        Any phase is accepted (DEFENSE submissions just can't reach the
        target service over the network, so this rarely matters in practice).
        The phase is carried in the event payload so consumers (panel,
        scoreboard) can render / weight it however they like.

        The flag string itself is never written to the event — the bus is
        fanned out to the public panel WebSocket and persisted to a JSONL
        log; leaking flags there would give every observer an
        attack-for-free.
        """
        result = self.flags.submit(team_id, flag)
        st = self.status()
        await self.bus.publish(
            new_event(
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
        )
        return result

    # ── helpers ─────────────────────────────────────────────────────────

    def _handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))