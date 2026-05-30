"""GameMgr — deploys challenges and drives the round loop.

Lifecycle:
  deploy_challenges()   -> sync: install + start every challenge on its target containers
  start()               -> async: wires file sink + runs the round loop until done/stop
  stop()                -> async: signals the round loop and closes the file sink
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path

import docker

from orchestrator.orchestrator import Orchestrator
from orchestrator.service import ContainerHandle, Service

from agents.prompts import build_attack_prompt, build_defense_prompt
from challenges.notes import BuiltinNotesSource
from challenges.registry import build as build_challenge
from challenges.source import ChallengeSpec, FlagMechanism, HealthLevel
from models.agent import AgentConfig
import shlex
from typing import Awaitable, Callable

from .artifacts import ArtifactKind, ArtifactStore

# Kickoff handler signature for persistent-memory v1. main.py registers
# one of these per phase via register_defense_kickoff_handler /
# register_attack_kickoff_handler; the handler awaits run_phase on
# ClaudeAgentManager and emits AGENT_COMPLETED / TIMEOUT / FAILED.
#   args: (team_id, cfg, round_id, phase_lower, is_first)
PhaseKickoffHandler = Callable[[str, AgentConfig, int, str, bool], Awaitable[None]]

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
        # Only SERVICE_PROTOCOL challenges (notes) populate this map; FILE_AT_PATH
        # challenges (nautilus-ico) ship the service as part of the image and
        # plant flags via direct docker-exec writes — they go in _spec_by_owner.
        self._service_by_owner: dict[tuple[str, str], Service] = {}
        # (team_id, service_name) -> ChallengeSpec, populated by deploy_challenges
        # for FILE_AT_PATH challenges. plant_flags walks this for direct
        # flag-file writes; emit_setup_events emits SERVICE_DEPLOYED for them.
        self._spec_by_owner: dict[tuple[str, str], ChallengeSpec] = {}
        # Container names that hosted FILE_AT_PATH challenge deploys — kept
        # so emit_setup_events can emit SERVICE_DEPLOYED uniformly.
        self._deployed_file_at_path: list[tuple[ChallengeSpec, list[str]]] = []
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
        # Persistent-memory v1: kickoff handlers run a phase via
        # ClaudeAgentManager.run_phase (not install). Handler signature:
        #   async def handler(team_id, cfg, round_id, phase, is_first)
        # is_first is True on the very first kickoff per team (any phase)
        # so the handler can route to the long-briefing prompt path;
        # subsequent kickoffs get a short cue. None = no-op (the kickoff
        # event still publishes; tests use this to verify event emission
        # without spinning up real agents).
        self._defense_kickoff_handler: "PhaseKickoffHandler | None" = None
        self._attack_kickoff_handler: "PhaseKickoffHandler | None" = None
        # Persistent-memory v1: one-shot install runs BEFORE the round
        # loop starts. Handler signature:
        #   async def handler(team_id, cfg) -> None
        # main.py registers a handler that bridges ClaudeAgentManager.install
        # (sync) into the loop via run_in_executor. None = event-only mode
        # (tests don't install real agents).
        self._install_agent_handler: (
            Callable[[str, AgentConfig], Awaitable[None]] | None
        ) = None
        # Idempotency keys per round so a re-fire of PHASE_CHANGE doesn't
        # spawn duplicate work.
        self._defense_kickoff_done: set[tuple[int, str, str]] = set()
        self._attack_kickoff_done: set[tuple[int, str]] = set()
        # Tracks team_ids that have already seen at least one kickoff.
        # Used to set the `is_first` flag on subsequent kickoffs — first
        # prompt for a team carries the full operator briefing; later
        # prompts are 2–3 line task cues (Claude --continue has the prior
        # session in memory by then).
        self._agent_briefed: set[str] = set()
        # Artifact persistence: ArtifactStore is built lazily in start()
        # once run_dir is known. Per-phase stream/agent/metadata files are
        # written by ClaudeAgentManager.run_phase directly into the
        # team×round directory; GameMgr only owns end-of-round service-log
        # snapshots (docker logs).
        self.artifacts: ArtifactStore | None = None
        # Defense-phase early-end tracking. On every DEFENSE_KICKOFF we add
        # (team_id, service) to the set for that round; DEPLOY_COMPLETE
        # drains it; when the set is empty the round loop is signalled to
        # cut DEFENSE short without waiting for the timer.
        self._defense_pending: dict[int, set[tuple[str, str]]] = {}
        # Subscribe at construction time (not start()): the subscribers
        # have no file-sink dependency and tests routinely drive
        # PHASE_CHANGE events directly onto the bus without the round loop.
        # ORDER MATTERS:
        #   _on_persist_service_logs     — snapshot prod-container docker logs
        #   _on_defense_end_health_check — final per-team health probe
        #   _on_defense_kickoff          — only fires on DEFENSE
        #   _on_attack_kickoff           — only fires on ATTACK
        #   _on_deploy_complete          — drains _defense_pending, may
        #                                  signal RoundLoop early-end
        self.bus.subscribe(self._on_persist_service_logs)
        self.bus.subscribe(self._on_defense_end_health_check)
        self.bus.subscribe(self._on_defense_kickoff)
        self.bus.subscribe(self._on_attack_kickoff)
        self.bus.subscribe(self._on_deploy_complete)

    # ── lifecycle ───────────────────────────────────────────────────────

    def deploy_challenges(self) -> None:
        """Install + start every challenge on its target containers.
        Runs synchronously before the round loop starts.

        Two deploy paths depending on `spec.flag_mechanism`:
        - SERVICE_PROTOCOL (e.g. notes): legacy Service-based install/start.
          The Service class ships the binary into the container at deploy
          time and plant_flag delivers the flag via the service's own
          protocol (e.g. PUT for notes).
        - FILE_AT_PATH (e.g. nautilus-ico): the service is already baked
          into the image; the entrypoint built it and started sshd. We
          just trigger `spec.metadata.restart_cmd` so the service binary
          is running with the (initial) source. The flag is written
          directly as a file by plant_flags.
        """

        team_to_prod = {t.id: t.prod for t in self.config.topology.teams}

        for chcfg in self.config.challenges:
            spec = self._resolve_spec(chcfg.name)
            mechanism = spec.flag_mechanism if spec is not None else FlagMechanism.SERVICE_PROTOCOL

            if mechanism == FlagMechanism.FILE_AT_PATH:
                # Image-bundled service. Trigger the framework restart_cmd
                # (e.g. /arena/restart.sh) so the service binary is running.
                restart_cmd = (spec.metadata or {}).get("restart_cmd")
                cnames: list[str] = []
                for team_id in chcfg.deploy_on:
                    cname = team_to_prod.get(team_id)
                    if cname is None:
                        raise KeyError(f"challenge {chcfg.name!r}: unknown team_id {team_id!r}")
                    log.info(
                        "deploying challenge %s -> %s (FILE_AT_PATH, port %d)",
                        chcfg.name, cname, chcfg.port,
                    )
                    if restart_cmd:
                        rc, out = self._exec_in_container(cname, restart_cmd)
                        if rc != 0:
                            log.warning(
                                "initial restart_cmd failed on %s rc=%d out=%r",
                                cname, rc, out[-200:] if out else "",
                            )
                    self._spec_by_owner[(team_id, chcfg.name)] = spec
                    cnames.append(cname)
                self._deployed_file_at_path.append((spec, cnames))
                continue

            # SERVICE_PROTOCOL path (legacy notes-style adapter).
            service = build_challenge(chcfg.type, port=chcfg.port)
            handles: list[ContainerHandle] = []
            for team_id in chcfg.deploy_on:
                cname = team_to_prod.get(team_id)
                if cname is None:
                    raise KeyError(f"challenge {chcfg.name!r}: unknown team_id {team_id!r}")
                handle = self.handle_for(cname)
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

        # SERVICE_PROTOCOL plants (notes: PUT over its own TCP socket).
        for (team_id, service_name), service in self._service_by_owner.items():
            cname = team_to_prod[team_id]
            handle = self.handle_for(cname)
            flag = new_flag()
            log.info("planting flag on %s/%s (round=%d, SERVICE_PROTOCOL)", team_id, service_name, round_id)
            service.plant_flag(handle, flag)
            self.flags.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))

        # FILE_AT_PATH plants (nautilus-ico: docker-exec write to /flag).
        for (team_id, service_name), spec in self._spec_by_owner.items():
            cname = team_to_prod[team_id]
            flag = new_flag()
            flag_path = (spec.metadata or {}).get("flag_path", "/flag")
            log.info(
                "planting flag on %s/%s -> %s (round=%d, FILE_AT_PATH)",
                team_id, service_name, flag_path, round_id,
            )
            self._plant_file_at_path(cname, flag_path, flag)
            self.flags.plant(team_id, service_name, flag)
            planted.append((team_id, service_name, flag))

        self._last_planted_round = round_id
        return planted

    def _plant_file_at_path(self, container_name: str, flag_path: str, flag: str) -> None:
        """Write `flag` (no trailing newline) to `flag_path` inside the
        container as root, mode 644. Raises if the write fails — a missing
        flag would silently break every CHECKER probe (ico reads /flag on
        CONNECT) and we want that surfaced loudly at deploy time.
        """
        # shlex.quote handles any quote/dollar/backtick in `flag` safely.
        cmd = (
            f"sh -c 'printf %s {shlex.quote(flag)} > {shlex.quote(flag_path)} "
            f"&& chmod 644 {shlex.quote(flag_path)}'"
        )
        rc, out = self._exec_in_container(container_name, cmd)
        if rc != 0:
            raise RuntimeError(
                f"FILE_AT_PATH plant failed on {container_name}:{flag_path}: "
                f"rc={rc}, out={out[-200:]!r}"
            )

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
        # Artifact persistence under the same run_dir as events.jsonl.
        # Each ArtifactKind gets a reserved subdir (created on construction);
        # api-logs / agent-logs / service-logs get filled as the round loop
        # runs. See gamemgr/artifacts.py for the layout.
        self.artifacts = ArtifactStore(self.run_dir)
        log.info("game start; events -> %s ; artifacts -> %s", self._file_sink.path, self.artifacts.root)

        await self.emit_setup_events()

        # Persistent-memory v1: one-shot install across all teams BEFORE
        # the round loop ticks. Parallel via asyncio.gather so a 10s npm
        # install doesn't serialize 2x across teams. No-op if no install
        # handler was registered (tests / observer-only runs).
        await self.install_all_agents()

        # Re-plant at each DEFENSE entry (M5.5). Sequential await in
        # bus.publish means this handler completes before _enter_phase
        # returns and the DEFENSE timer begins.
        self.bus.subscribe(self._on_rotation_event)
        # NB: _on_defense_kickoff is subscribed in __init__ (it doesn't
        # depend on file-sink state and tests need it active without start()).

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
        for spec, cnames in self._deployed_file_at_path:
            for cn in cnames:
                await self.bus.publish(
                    new_event(
                        EventType.SERVICE_DEPLOYED,
                        payload={"service": spec.name, "container": cn, "port": spec.port},
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

    # ── Persistent-memory v1: pre-loop install hook ─────────────────────

    def register_install_agent_handler(
        self,
        handler: Callable[[str, AgentConfig], Awaitable[None]],
    ) -> None:
        """Register the callable that one-shot-installs the agent on a
        team's dev container. Called once per team by `install_all_agents()`
        BEFORE the round loop starts.

        Signature: `async def handler(team_id, cfg) -> None`.

        Without a handler, `install_all_agents` no-ops (event-only mode
        for tests that drive the round loop without real agents).
        """
        self._install_agent_handler = handler

    async def install_all_agents(self) -> None:
        """Pre-loop one-shot install across every team in parallel.

        Persistent-memory v1: install creates the workdir, runs npm install,
        provisions SSH pivot, writes agent.env / mcp.json. No process is
        launched — that happens per-phase via `run_phase` inside the
        kickoff handlers.

        Best-effort: a team whose install raises gets a log line and is
        skipped — the round loop still starts and the failing team's
        kickoff will fail with a clean AGENT_FAILED on the bus.
        """
        if self._install_agent_handler is None:
            return  # event-only mode (tests)
        handler = self._install_agent_handler

        async def _one(team_id: str) -> None:
            try:
                cfg = self.agent_config_for(team_id, is_first=True)
            except KeyError:
                log.warning(
                    "install_all_agents: no agent config for team %s; skipping",
                    team_id,
                )
                return
            try:
                await handler(team_id, cfg)
            except Exception:
                log.exception("install_all_agents: install failed for %s", team_id)

        await asyncio.gather(
            *(_one(t.id) for t in self.config.topology.teams)
        )

    # ── DEFCON: defense kickoff on PHASE_CHANGE → DEFENSE ───────────────

    def register_defense_kickoff_handler(
        self,
        handler: "PhaseKickoffHandler",
    ) -> None:
        """Register the callable that runs the per-phase defense agent.

        Signature: `async def handler(team_id, cfg, round_id, phase, is_first)`.
        Called once per (team, deployable defense challenge) on every
        DEFENSE phase entry, after flags rotate. `is_first` is True on
        the very first kickoff for a team (any phase) so the handler
        can route to the long briefing prompt; subsequent kickoffs get
        a short cue. `phase` is the lowercase string `"defense"` so the
        handler can pick the right artifact filename.

        Kept external (rather than baked into GameMgr) so the round loop
        is unchanged for non-defense use cases (unit tests, screenshots,
        attack-only smoke runs) — they construct GameMgr without a handler
        and the kickoff devolves to event emission only.

        Replaces any previously-registered handler.
        """
        self._defense_kickoff_handler = handler

    async def _on_defense_kickoff(self, event) -> None:
        """Subscriber: on every DEFENSE entry, emit DEFENSE_KICKOFF per
        (team, defense-capable service) and invoke the registered handler.

        A challenge is "defense-capable" iff its spec carries a restart_cmd
        in metadata (i.e. it can be rebuilt in-place). Toy challenges
        without that — notes today — don't get a defense agent; this keeps
        the kickoff signal honest with respect to what the framework can
        actually defend.
        """
        if event.type != EventType.PHASE_CHANGE.value:
            return
        if event.phase != Phase.DEFENSE.value:
            return

        # Collect handler invocations, then run them concurrently so per-team
        # work doesn't serialize across teams. is_first is decided AT
        # collection time (sync) so the prompt-builder inside
        # defense_agent_config_for emits a short cue when the team has
        # already been briefed earlier in the game.
        pending: list[tuple[str, str, AgentConfig, bool]] = []
        for chcfg in self.config.challenges:
            spec = self._resolve_spec(chcfg.name)
            if spec is None:
                continue
            if not (spec.metadata or {}).get("restart_cmd"):
                continue  # not defense-capable

            for team_id in chcfg.deploy_on:
                key = (event.round_id, team_id, chcfg.name)
                if key in self._defense_kickoff_done:
                    continue
                self._defense_kickoff_done.add(key)

                await self.bus.publish(
                    new_event(
                        EventType.DEFENSE_KICKOFF,
                        team_id=team_id,
                        round_id=event.round_id,
                        phase=event.phase,
                        payload={"service": chcfg.name},
                    )
                )
                # Track expected DEPLOY_COMPLETE. _on_deploy_complete drains
                # this; when empty for the current round we cut the
                # DEFENSE timer short.
                self._defense_pending.setdefault(event.round_id, set()).add(
                    (team_id, chcfg.name)
                )

                if self._defense_kickoff_handler is None:
                    continue

                is_first = team_id not in self._agent_briefed
                # Add immediately so a second defense-capable service for
                # the same team in this loop gets is_first=False.
                self._agent_briefed.add(team_id)

                try:
                    cfg = self.defense_agent_config_for(
                        team_id, service_name=chcfg.name, is_first=is_first,
                    )
                except Exception:
                    log.exception(
                        "defense_agent_config_for failed for (%s, %s)",
                        team_id, chcfg.name,
                    )
                    continue
                pending.append((team_id, chcfg.name, cfg, is_first))

        if pending and self._defense_kickoff_handler is not None:
            handler = self._defense_kickoff_handler
            round_id = event.round_id
            async def _safe(team_id: str, service_name: str, cfg: AgentConfig, is_first: bool) -> None:
                try:
                    await handler(team_id, cfg, round_id, "defense", is_first)
                except Exception:
                    log.exception(
                        "defense kickoff handler raised for (%s, %s); skipping",
                        team_id, service_name,
                    )
            await asyncio.gather(*(_safe(t, s, c, f) for t, s, c, f in pending))

    # ── DEFCON: attack kickoff on PHASE_CHANGE → ATTACK ─────────────────

    def register_attack_kickoff_handler(
        self,
        handler: "PhaseKickoffHandler",
    ) -> None:
        """Register the callable that runs the per-phase attack agent.

        Signature: `async def handler(team_id, cfg, round_id, phase, is_first)`.
        Called once per team on every ATTACK phase entry. `is_first` is
        True on the very first kickoff for a team (any phase).
        """
        self._attack_kickoff_handler = handler

    async def _on_attack_kickoff(self, event) -> None:
        """Subscriber: on every ATTACK entry, emit ATTACK_KICKOFF per team
        and invoke the registered handler.

        Per team (not per-team-per-service) because the M5.6 attack prompt
        walks every opposing prod from a single install. Idempotent per
        (round_id, team_id) so a re-fire of PHASE_CHANGE in the same round
        doesn't double-spawn.
        """
        if event.type != EventType.PHASE_CHANGE.value:
            return
        if event.phase != Phase.ATTACK.value:
            return

        pending: list[tuple[str, AgentConfig, bool]] = []
        for team in self.config.topology.teams:
            key = (event.round_id, team.id)
            if key in self._attack_kickoff_done:
                continue
            self._attack_kickoff_done.add(key)

            await self.bus.publish(
                new_event(
                    EventType.ATTACK_KICKOFF,
                    team_id=team.id,
                    round_id=event.round_id,
                    phase=event.phase,
                    payload={},
                )
            )

            if self._attack_kickoff_handler is None:
                continue

            is_first = team.id not in self._agent_briefed
            self._agent_briefed.add(team.id)

            try:
                cfg = self.agent_config_for(team.id, is_first=is_first)
            except Exception:
                log.exception("agent_config_for failed for %s; skipping attack kickoff", team.id)
                continue
            pending.append((team.id, cfg, is_first))

        if pending and self._attack_kickoff_handler is not None:
            handler = self._attack_kickoff_handler
            round_id = event.round_id
            async def _safe(team_id: str, cfg: AgentConfig, is_first: bool) -> None:
                try:
                    await handler(team_id, cfg, round_id, "attack", is_first)
                except Exception:
                    log.exception(
                        "attack kickoff handler raised for %s; skipping",
                        team_id,
                    )
            await asyncio.gather(*(_safe(t, c, f) for t, c, f in pending))

    # ── DEFCON: end-of-round service-log snapshots ──────────────────────

    async def _on_persist_service_logs(self, event) -> None:
        """At ROUND_STOP, snapshot every team's prod container `docker logs`
        to service-logs.

        Per-phase agent transcripts (stream-{phase}.jsonl / agent-{phase}.log
        / metadata-{phase}.json) are written directly by
        ClaudeAgentManager.run_phase into the team×round artifact dir — no
        GameMgr subscriber needed for that path.
        """
        if self.artifacts is None:
            return
        if event.type != EventType.ROUND_STOP.value:
            return
        await self._persist_service_logs(event.round_id)

    async def _persist_service_logs(self, round_id: int) -> None:
        """Snapshot `docker logs <team{N}_prod>` for every team. End-of-round
        only — captures the prod container's stdout/stderr across DEFENSE +
        ATTACK in one file (timestamps from docker preserve sub-phase ordering).
        """
        if self.artifacts is None:
            return
        client = docker.from_env()
        for team in self.config.topology.teams:
            if not team.prod:
                continue
            try:
                c = client.containers.get(team.prod)
                blob = c.logs(timestamps=True).decode(errors="replace")
            except Exception:
                log.exception("artifact: docker logs failed for %s", team.prod)
                continue
            try:
                p = self.artifacts.path_for(
                    ArtifactKind.SERVICE_LOG,
                    team_id=team.id,
                    round_id=round_id,
                    filename="docker.log",
                )
                p.write_text(blob)
            except Exception:
                log.exception(
                    "artifact: write SERVICE_LOG failed for %s round=%d",
                    team.id, round_id,
                )

    # ── DEFCON: defense-phase early-end + end-of-defense health check ───

    async def _on_deploy_complete(self, event) -> None:
        """When an agent self-reports DEPLOY_COMPLETE for the current round,
        drain `_defense_pending`. If every defense-capable (team, service)
        is done, tell the round loop to cut the DEFENSE timer short.

        Note: DEPLOY_COMPLETE arrives via MCP from the agent (see
        gamemgr/mcp_server.py:report_deploy_complete). The host doesn't
        cross-check that the agent actually succeeded — it trusts the
        report. The end-of-defense health check in `_on_defense_end_health_check`
        is the host's independent verification.
        """
        if event.type != EventType.DEPLOY_COMPLETE.value:
            return
        team_id = event.team_id
        service = (event.payload or {}).get("service")
        if team_id is None or service is None:
            return
        pending = self._defense_pending.get(event.round_id)
        if pending is None:
            return  # no defense kickoff was recorded for this round
        pending.discard((team_id, service))
        if not pending and self.round_loop is not None:
            log.info(
                "all defense-capable teams reported DEPLOY_COMPLETE for round %d; "
                "ending DEFENSE phase early",
                event.round_id,
            )
            self.round_loop.end_phase_early()

    async def _on_defense_end_health_check(self, event) -> None:
        """At the DEFENSE→ATTACK transition, run a final 4-level health
        chain for every (team, defense-capable service) — independent of
        whether the team called restart_service this round.

        Two reasons to do this:
        1. A team that skipped restart_service entirely still gets a
           CHECKER probe — catches "defense by inaction" attempts.
        2. The DEPLOY_COMPLETE-driven early-end above trusts the agent's
           report. The independent health check is the verification:
           passed=true at level=checker confirms the patch holds; failed
           tells the operator the agent's claim was hollow.
        """
        if event.type != EventType.PHASE_CHANGE.value:
            return
        if event.phase != Phase.ATTACK.value:
            return  # we want the DEFENSE→ATTACK edge

        tasks = []
        for chcfg in self.config.challenges:
            spec = self._resolve_spec(chcfg.name)
            if spec is None:
                continue
            if not (spec.metadata or {}).get("restart_cmd"):
                continue
            for team_id in chcfg.deploy_on:
                team = next((t for t in self.config.topology.teams if t.id == team_id), None)
                if team is None or not team.prod:
                    continue
                tasks.append(self._emit_health_chain(
                    team_id=team_id,
                    service=chcfg.name,
                    container_name=team.prod,
                    spec=spec,
                ))
        if tasks:
            await asyncio.gather(*tasks)

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

    # ── DEFCON: restart_service + health checks ─────────────────────────

    def _resolve_spec(self, service_name: str) -> ChallengeSpec | None:
        """Look up the ChallengeSpec by name from the configured sources.

        Lazy-cached on first call so subsequent restarts don't re-enumerate.
        Sources are discovered as needed:
          - BuiltinNotesSource is always present (notes is the toy challenge)
          - NautilusSource is attached if vendor/nautilus-finals-2025 exists
            and includes the matching upstream dir (inferred from any
            non-notes ChallengeConfig in game.yaml).
        """
        cache = getattr(self, "_spec_cache", None)
        if cache is None:
            cache = {}
            # Notes is always available.
            for spec in BuiltinNotesSource().list_challenges():
                cache[spec.name] = spec
            # Nautilus: instantiate only if at least one configured challenge
            # name starts with "nautilus-". (We don't speculatively pull the
            # vendor repo just to enumerate.)
            nautilus_names = [
                c.name[len("nautilus-"):]
                for c in self.config.challenges
                if c.name.startswith("nautilus-")
            ]
            if nautilus_names:
                # Lazy import to avoid pulling docker SDK during config-only flows.
                from challenges.nautilus import NautilusSource

                try:
                    for spec in NautilusSource(include=nautilus_names).list_challenges():
                        cache[spec.name] = spec
                except FileNotFoundError as e:
                    log.warning("nautilus source unavailable: %s", e)
            self._spec_cache = cache
        return cache.get(service_name)

    async def restart_service(
        self,
        team_id: str,
        service: str,
    ) -> str:
        """Async restart of `service` on `team_id`'s prod container.

        Returns a `job_id` immediately; the rebuild + restart runs in a
        background task that emits SERVICE_RESTART_STARTED / _COMPLETED /
        _FAILED + a SERVICE_HEALTH_CHECK per level on the bus.

        Raises ValueError for unknown service, no restart_cmd in spec, or
        a restart already in flight for this (team, service). The MCP
        layer translates these into InvalidParams.
        """
        # 1. Spec lookup + restart_cmd resolution.
        spec = self._resolve_spec(service)
        if spec is None:
            raise ValueError(f"unknown service: {service!r}")
        restart_cmd = spec.metadata.get("restart_cmd") if spec.metadata else None
        if not restart_cmd:
            raise ValueError(
                f"service {service!r} has no restart_cmd in spec — not defendable"
            )

        # 2. Container lookup. M5.6 topology: one team{N}_prod per team.
        team = next((t for t in self.config.topology.teams if t.id == team_id), None)
        if team is None or not team.prod:
            raise ValueError(f"unknown team: {team_id!r}")
        container_name = team.prod

        # 3. In-flight guard — one rebuild per (team, service) at a time.
        if not hasattr(self, "_restart_jobs"):
            self._restart_jobs = {}  # job_id → metadata
        for existing_job_id, meta in self._restart_jobs.items():
            if meta["team_id"] == team_id and meta["service"] == service:
                if not meta.get("done"):
                    raise ValueError(
                        f"restart already in flight for ({team_id!r}, {service!r}): job={existing_job_id}"
                    )

        # 4. Emit STARTED + spawn the background task.
        job_id = secrets.token_hex(6)  # 12-hex
        started_at = time.time()
        self._restart_jobs[job_id] = {
            "team_id": team_id, "service": service,
            "started_at": started_at, "done": False,
        }
        st = self.status()
        await self.bus.publish(
            new_event(
                EventType.SERVICE_RESTART_STARTED,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={
                    "job_id": job_id,
                    "team_id": team_id,
                    "service": service,
                    "started_at": started_at,
                },
            )
        )

        # Spawn the background restart task on the running event loop.
        asyncio.create_task(
            self._run_restart_job(
                job_id=job_id,
                team_id=team_id,
                service=service,
                container_name=container_name,
                restart_cmd=restart_cmd,
                spec=spec,
            )
        )
        return job_id

    async def _run_restart_job(
        self,
        *,
        job_id: str,
        team_id: str,
        service: str,
        container_name: str,
        restart_cmd: str,
        spec: ChallengeSpec,
    ) -> None:
        """Run restart_cmd inside the team's prod container via docker exec,
        emit COMPLETED/FAILED with duration_ms, then walk the 4 health levels."""
        started_at = self._restart_jobs[job_id]["started_at"]
        st = self.status()
        # docker exec is blocking; run it in the default thread pool so
        # we don't block the event loop while make/fpc/etc. runs.
        loop = asyncio.get_running_loop()
        try:
            exit_code, output = await loop.run_in_executor(
                None, self._exec_in_container, container_name, restart_cmd
            )
            duration_ms = int((time.time() - started_at) * 1000)
            if exit_code != 0:
                await self.bus.publish(
                    new_event(
                        EventType.SERVICE_RESTART_FAILED,
                        team_id=team_id,
                        round_id=st.get("round", 0),
                        phase=st.get("phase", ""),
                        payload={
                            "job_id": job_id,
                            "team_id": team_id,
                            "service": service,
                            "duration_ms": duration_ms,
                            "error": (output[-200:] if output else f"rc={exit_code}"),
                        },
                    )
                )
                self._restart_jobs[job_id]["done"] = True
                return

            await self.bus.publish(
                new_event(
                    EventType.SERVICE_RESTART_COMPLETED,
                    team_id=team_id,
                    round_id=st.get("round", 0),
                    phase=st.get("phase", ""),
                    payload={
                        "job_id": job_id,
                        "team_id": team_id,
                        "service": service,
                        "duration_ms": duration_ms,
                    },
                )
            )

            # Health check chain — emit one SERVICE_HEALTH_CHECK per level.
            await self._emit_health_chain(
                team_id=team_id,
                service=service,
                container_name=container_name,
                spec=spec,
            )
        except Exception as e:
            duration_ms = int((time.time() - started_at) * 1000)
            await self.bus.publish(
                new_event(
                    EventType.SERVICE_RESTART_FAILED,
                    team_id=team_id,
                    round_id=st.get("round", 0),
                    phase=st.get("phase", ""),
                    payload={
                        "job_id": job_id,
                        "team_id": team_id,
                        "service": service,
                        "duration_ms": duration_ms,
                        "error": f"{type(e).__name__}: {e}"[:200],
                    },
                )
            )
        finally:
            self._restart_jobs[job_id]["done"] = True

    def _exec_in_container(self, container_name: str, cmd: str) -> tuple[int, str]:
        """Run `cmd` inside the container via docker exec (blocking).
        Called from the thread pool by `_run_restart_job`."""
        client = docker.from_env()
        c = client.containers.get(container_name)
        result = c.exec_run(["bash", "-c", cmd])
        out = result.output.decode(errors="replace") if result.output else ""
        return result.exit_code, out

    async def _emit_health_chain(
        self,
        *,
        team_id: str,
        service: str,
        container_name: str,
        spec: ChallengeSpec,
    ) -> None:
        """Run PROCESS → TCP → PROTOCOL → CHECKER, emit one event each.

        PROCESS: pgrep against spec.metadata.health_check.process_pattern
        TCP: orchestrator-side socket connect to spec.port (via docker exec python)
        PROTOCOL / CHECKER: defer to a Service instance if available; otherwise
          skip with a "no service adapter wired" detail and passed=False.
        """
        st = self.status()
        hc = (spec.metadata or {}).get("health_check") or {}

        async def _emit(level: HealthLevel, passed: bool, detail: str) -> None:
            await self.bus.publish(
                new_event(
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
            )

        loop = asyncio.get_running_loop()

        # PROCESS
        process_pattern = hc.get("process_pattern")
        if process_pattern:
            rc, out = await loop.run_in_executor(
                None, self._exec_in_container, container_name,
                f"pgrep -f {process_pattern!r}"
            )
            await _emit(HealthLevel.PROCESS, rc == 0, f"pgrep rc={rc}")
        else:
            await _emit(HealthLevel.PROCESS, True, "no process_pattern; skipped")

        # TCP
        tcp_probe = (
            f"python3 -c \"import socket; s=socket.socket(); s.settimeout(2); "
            f"s.connect(('127.0.0.1', {spec.port})); s.close()\""
        )
        rc, _ = await loop.run_in_executor(
            None, self._exec_in_container, container_name, tcp_probe
        )
        await _emit(HealthLevel.TCP, rc == 0, f"socket-connect rc={rc}")

        # PROTOCOL — defer to Service.is_alive(PROTOCOL) when we have an
        # adapter wired (deploy_challenges populates _service_by_owner).
        # If no adapter (e.g. nautilus-ico tested standalone, no Service yet),
        # fall back to "passed=True with reason" — the deeper CHECKER below
        # still runs and provides the load-bearing gameplay-correctness signal.
        proto_ok = True
        service_obj = self._service_by_owner.get((team_id, service))
        if service_obj is not None:
            handle = self.handle_for(container_name)
            try:
                proto_ok = await loop.run_in_executor(
                    None, lambda: service_obj.is_alive(handle, HealthLevel.PROTOCOL)
                )
                await _emit(HealthLevel.PROTOCOL, bool(proto_ok), f"is_alive(PROTOCOL)={proto_ok}")
            except Exception as e:
                await _emit(HealthLevel.PROTOCOL, False, f"is_alive PROTOCOL raised: {type(e).__name__}")
                proto_ok = False
        else:
            await _emit(HealthLevel.PROTOCOL, True, "no service adapter; deferred to CHECKER")

        # CHECKER — for nautilus challenges, runs the framework-installed
        # checker.sh inside the container. For notes (no checker_command),
        # aliases to PROTOCOL — documented honest fallback, no separable
        # gameplay surface.
        checker_cmd = hc.get("checker_command")
        if checker_cmd:
            rc, out = await loop.run_in_executor(
                None, self._exec_in_container, container_name, checker_cmd
            )
            detail = (out.strip() or f"rc={rc}")[-200:]
            await _emit(HealthLevel.CHECKER, rc == 0, detail)
        else:
            await _emit(HealthLevel.CHECKER, bool(proto_ok), "no checker_command; aliased to PROTOCOL")

    # ── agent provisioning (M5.6) ───────────────────────────────────────

    def agent_config_for(
        self,
        team_id: str,
        submit_base_url: str = "http://host.docker.internal:8000",
        *,
        is_first: bool = True,
    ) -> AgentConfig:
        """Build the per-team AgentConfig for attack-mode install.

        Pulls the static cli/model/api_key/base_url from config.agents[team_id]
        (loaded from game.yaml) and layers on the runtime topology context
        (own prod, opposing prods, token, submit URL, attack prompt).

        `is_first=True` (default) emits the long briefing — used for
        install AND for the first attack-phase kickoff. `is_first=False`
        emits the 4-line continuation cue used on every subsequent
        kickoff for this team (Claude's `--continue` carries the prior
        context).

        The attack prompt is picked per-challenge via the prompts dispatcher.
        v1 assumes one challenge configured per game; if there's exactly one
        in `config.challenges`, we pass its name to the dispatcher. If
        multiple are configured, the dispatcher's notes-recipe fallback runs.
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
        # Pick a service name for the attack prompt — the single configured
        # challenge for this team. (Multi-challenge games would need an
        # explicit per-team mapping; deferred.)
        attack_service: str | None = None
        deployable = [c for c in self.config.challenges if team_id in c.deploy_on]
        if len(deployable) == 1:
            attack_service = deployable[0].name
        return replace(
            cfg,
            system_prompt=build_attack_prompt(
                cfg, service_name=attack_service, is_first=is_first
            ),
        )

    def defense_agent_config_for(
        self,
        team_id: str,
        *,
        service_name: str,
        submit_base_url: str = "http://host.docker.internal:8000",
        is_first: bool = True,
    ) -> AgentConfig:
        """Build the per-team AgentConfig for DEFENSE mode (DEFCON Phase 4b).

        Mirrors `agent_config_for` but swaps the prompt for the
        edit→restart→smoke→report recipe via `build_defense_prompt`. Pulls
        port + source_view from the resolved ChallengeSpec so the prompt
        points the agent at the right path inside its own prod container.
        """
        base = self.config.agents.get(team_id)
        if base is None:
            raise KeyError(f"no agent config for team {team_id!r} in game.yaml")

        teams_by_id = {t.id: t for t in self.config.topology.teams}
        if team_id not in teams_by_id:
            raise KeyError(f"unknown team_id {team_id!r}")
        own_team = teams_by_id[team_id]

        spec = self._resolve_spec(service_name)
        if spec is None:
            raise ValueError(f"unknown service: {service_name!r}")

        from .mcp_server import canonical_endpoint_url

        cfg = replace(
            base,
            team_id=team_id,
            team_token=own_team.token,
            own_prod_host=own_team.prod,
            own_prod_container_name=own_team.prod,
            # Defense doesn't pivot to opposing prods — leave empty.
            opposing_prod_hosts=tuple(),
            submit_url=f"{submit_base_url.rstrip('/')}/game/flag/submit",
            mcp_endpoint=canonical_endpoint_url(submit_base_url),
        )
        return replace(
            cfg,
            system_prompt=build_defense_prompt(
                cfg,
                service_name=service_name,
                port=spec.port,
                is_first=is_first,
            ),
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))