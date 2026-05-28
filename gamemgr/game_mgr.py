"""GameMgr — deploys challenges and drives the round loop.

Lifecycle:
  deploy_challenges()   -> sync: install + start every challenge on its target containers
  start()               -> async: wires file sink + runs the round loop until done/stop
  stop()                -> async: signals the round loop and closes the file sink
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from orchestrator.orchestrator import Orchestrator
from orchestrator.service import ContainerHandle, Service

from agents.prompts import build_attack_prompt
from challenges.registry import build as build_challenge
from dataclasses import replace
from models.agent import AgentConfig

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
        self._last_planted_round: int = -1
        # token -> team_id, populated from config; lookup for API auth.
        self._token_to_team: dict[str, str] = {
            t.token: t.id for t in config.topology.teams if t.token
        }
        # M4.5: agent self-reports via MCP. Overwrite-per-(team, service);
        # only the latest deploy report survives. Adaptive defense duration
        # (future) will read duration_ms; nothing in this milestone reads it.
        self.deploy_reports: dict[str, dict[str, dict]] = {}
        # M4.5: service-name whitelist built from yaml `challenges:`. Plugin
        # model intact — we never compare against a hardcoded literal.
        self._known_services: frozenset[str] = frozenset(
            c.name for c in config.challenges
        )

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
        """Pre-game plant (round_id=0). Same as `plant_flags(0)`; kept under
        its own name because main.py and the M5 tests call it before the
        round loop starts."""
        return self.plant_flags(round_id=0)

    def plant_flags(self, round_id: int) -> list[tuple[str, str, str]]:
        """Re-plant one fresh flag per (team, deployed service).

        Called once at pre-game (round_id=0) by main.py, then again at the
        start of every DEFENSE phase by the rotation subscriber installed in
        `start()`. `FlagStore.plant()` drops the prior flag from the inverted
        index so it can no longer be submitted — rotation invalidates stale
        flags immediately, by construction.

        Returns the list of (team_id, service_name, flag) planted (handy
        for tests). Per-round rotation is the M5.5 addition; the design
        kept this one-shot in M5 but the store API was already shaped for it.
        """
        planted: list[tuple[str, str, str]] = []
        team_to_prod = {t.id: t.prod for t in self.config.topology.teams}

        for (team_id, service_name), service in self._service_by_owner.items():
            cname = team_to_prod[team_id]
            handle = self._handle_for(cname)
            flag = new_flag()
            log.info("planting flag on %s/%s (round=%d)", team_id, service_name, round_id)
            service.plant_flag(handle, flag)
            self.flags.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))
        self._last_planted_round = round_id
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

        await self.emit_setup_events()

        # Re-plant at each DEFENSE entry (M5.5). Sequential await in
        # bus.publish means this handler completes before _enter_phase
        # returns and the DEFENSE timer begins.
        self.bus.subscribe(self._on_rotation_event)

        self.round_loop = RoundLoop(self.bus, self.orchestrator.network, self.config.rounds)
        await self.round_loop.run()

    async def emit_setup_events(self) -> None:
        """Publish one SERVICE_DEPLOYED per challenge-on-container and one
        FLAG_PLANTED per (team, service) at round_id=0.

        Called from start() during normal game lifecycle, but also exposed
        for demos / panel screenshots that don't drive the round loop —
        without this they'd see an empty event stream even though the
        underlying state (deployed services, planted flags) is populated.
        Flag strings are deliberately omitted from FLAG_PLANTED payloads
        (the bus is broadcast to public observers)."""
        for service, handles in self._deployed:
            for h in handles:
                await self.bus.publish(
                    new_event(
                        EventType.SERVICE_DEPLOYED,
                        payload={"service": service.name, "container": h.name, "port": service.port},
                    )
                )
        for (team_id, service_name) in self.flags.planted.keys():
            await self.bus.publish(
                new_event(
                    EventType.FLAG_PLANTED,
                    team_id=team_id,
                    round_id=0,
                    payload={"service": service_name},
                )
            )

    async def _on_rotation_event(self, event) -> None:
        """Subscriber: when PHASE_CHANGE → DEFENSE fires, rotate all flags."""
        if event.type != EventType.PHASE_CHANGE.value:
            return
        if event.phase != Phase.DEFENSE.value:
            return
        if event.round_id == self._last_planted_round:
            return  # already planted for this round (defensive against re-fire)
        planted = self.plant_flags(round_id=event.round_id)
        for team_id, service_name, _flag in planted:
            await self.bus.publish(
                new_event(
                    EventType.FLAG_PLANTED,
                    team_id=team_id,
                    round_id=event.round_id,
                    payload={"service": service_name},
                )
            )

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

    # ── M4.5: lifecycle / report emitters ───────────────────────────────

    def is_known_service(self, name: str) -> bool:
        """White-list check for agent-self-reported service names.

        Source of truth is `game.yaml.challenges:` (loaded into
        `config.challenges`). Anything else gets rejected before it reaches
        deploy_reports — adaptive-duration aggregation downstream depends
        on this being clean.
        """
        return name in self._known_services

    async def record_agent_installed(self, team_id: str) -> None:
        """Host emit: install preparation finished (files / env / mcp.json /
        ssh ready), supervisor NOT yet launched."""
        await self.bus.publish(
            new_event(
                EventType.AGENT_INSTALLED,
                team_id=team_id,
                payload={"team_id": team_id, "ts": time.time()},
            )
        )

    async def record_agent_launched(self, team_id: str, wrapper_pid: int) -> None:
        """Host emit: supervisor process running, wrapper pid captured."""
        await self.bus.publish(
            new_event(
                EventType.AGENT_LAUNCHED,
                team_id=team_id,
                payload={"team_id": team_id, "wrapper_pid": wrapper_pid, "ts": time.time()},
            )
        )

    async def record_agent_ready(self, team_id: str) -> None:
        """MCP-driven emit: LLM successfully called `report_ready`. Confirms
        the MCP channel works end-to-end. Best-effort — never load-bearing."""
        await self.bus.publish(
            new_event(
                EventType.AGENT_READY,
                team_id=team_id,
                payload={"team_id": team_id, "ts": time.time()},
            )
        )

    async def record_deploy_complete(
        self, team_id: str, service: str, duration_ms: int | None
    ) -> None:
        """MCP-driven emit: agent finished its defense deploy. Overwrite-per-
        (team, service) in deploy_reports; emit `DEPLOY_COMPLETE` event.

        Caller must have already validated `service` via `is_known_service()`.
        """
        completed_at = time.time()
        self.deploy_reports.setdefault(team_id, {})[service] = {
            "duration_ms": duration_ms,
            "completed_at": completed_at,
        }
        st = self.status()
        await self.bus.publish(
            new_event(
                EventType.DEPLOY_COMPLETE,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={
                    "team_id": team_id,
                    "service": service,
                    "duration_ms": duration_ms,
                    "completed_at": completed_at,
                },
            )
        )

    # ── agent provisioning (M5.6) ───────────────────────────────────────

    def agent_config_for(
        self,
        team_id: str,
        submit_base_url: str = "http://host.docker.internal:8000",
    ) -> AgentConfig:
        """Build the per-team AgentConfig for attack-mode install.

        Pulls the static cli/model/api_key/base_url from config.agents[team_id]
        (loaded from game.yaml) and layers on the runtime topology context
        (own prod, opposing prods, token, submit URL, attack prompt).
        """
        base = self.config.agents.get(team_id)
        if base is None:
            raise KeyError(f"no agent config for team {team_id!r} in game.yaml")

        teams_by_id = {t.id: t for t in self.config.topology.teams}
        if team_id not in teams_by_id:
            raise KeyError(f"unknown team_id {team_id!r}")
        own_team = teams_by_id[team_id]
        opposing = tuple(t.prod for tid, t in teams_by_id.items() if tid != team_id)

        # Import locally to avoid a top-of-module circular: mcp_server
        # imports GameMgr (via TYPE_CHECKING) for the type annotation.
        from .mcp_server import canonical_endpoint_url

        cfg = replace(
            base,
            team_id=team_id,
            team_token=own_team.token,
            own_prod_host=own_team.prod,
            own_prod_container_name=own_team.prod,
            opposing_prod_hosts=opposing,
            submit_url=f"{submit_base_url.rstrip('/')}/game/flag/submit",
            mcp_endpoint=canonical_endpoint_url(submit_base_url),
        )
        # Attack prompt is derived from the now-populated fields.
        return replace(cfg, system_prompt=build_attack_prompt(cfg))

    # ── helpers ─────────────────────────────────────────────────────────

    def _handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))