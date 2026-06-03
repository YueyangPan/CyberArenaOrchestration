"""GameMgr — deploys challenges and drives the round loop.

Lifecycle:
  deploy_challenges()   -> sync: install + start every challenge on its target containers
  start()               -> async: wires file sink + runs the round loop until done/stop
  stop()                -> async: signals the round loop and closes the file sink
"""

from __future__ import annotations

import asyncio
import json
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
from challenges.source import ChallengeSpec, FlagMechanism
from models.agent import AgentConfig
from typing import Awaitable, Callable

from .artifacts import ArtifactKind, ArtifactStore

# Kickoff handler signature for persistent-memory v1. main.py registers
# one of these per phase via register_defense_kickoff_handler /
# register_attack_kickoff_handler; the handler awaits run_phase on
# ClaudeAgentManager and emits AGENT_COMPLETED / TIMEOUT / FAILED.
#   args: (team_id, cfg, round_id, phase_lower, is_first)
PhaseKickoffHandler = Callable[[str, AgentConfig, int, str, bool], Awaitable[None]]

from .config import ChallengeConfig, GameConfig
from .events import EventType, new_event, new_run_dir
from .round import Phase, RoundLoop
from .timeline import Timeline

from flag import FlagServer, SubmitResult
from health.checker import HealthChecker
from health.poller import HealthPoller, build_targets

log = logging.getLogger(__name__)


class GameMgr:
    def __init__(self, config: GameConfig, orchestrator: Orchestrator) -> None:
        self.config = config
        self.orchestrator = orchestrator
        # Timeline replaces the legacy EventBus. It is observation-only:
        # FileSink + WebSocket consume; nothing internal subscribes.
        # Internal control flow (phase transitions, deploy_complete handling,
        # dry-run gating) goes through direct method calls on GameMgr.
        self.timeline = Timeline()
        self.round_loop: RoundLoop | None = None
        self.run_dir: Path | None = None
        self._deployed: list[tuple[Service, list[ContainerHandle]]] = []
        # (team_id, service_name) -> Service instance, populated by deploy_challenges
        # so the FlagServer can find the right adapter per (team, service).
        # Only SERVICE_PROTOCOL challenges (notes) populate this map; FILE_AT_PATH
        # challenges (nautilus-ico) ship the service as part of the image and
        # plant flags via direct docker-exec writes — they go in _spec_by_owner.
        self._service_by_owner: dict[tuple[str, str], Service] = {}
        # (team_id, service_name) -> ChallengeSpec, populated by deploy_challenges
        # for FILE_AT_PATH challenges. The FlagServer walks this for direct
        # flag-file writes; emit_setup_events emits SERVICE_DEPLOYED for them.
        self._spec_by_owner: dict[tuple[str, str], ChallengeSpec] = {}
        # Container names that hosted FILE_AT_PATH challenge deploys — kept
        # so emit_setup_events can emit SERVICE_DEPLOYED uniformly.
        self._deployed_file_at_path: list[tuple[ChallengeSpec, list[str]]] = []
        # FlagServer owns all flag mechanics (planting, rotation, submission
        # verification). GameMgr keeps the public method shapes
        # (plant_initial_flags / plant_flags / submit_flag / flags) but
        # delegates the work. Built here because the dicts above are passed
        # by reference — deploy_challenges populates them later, and the
        # FlagServer sees those mutations naturally.
        self.flag_server = FlagServer(
            self.timeline,
            topology=config.topology,
            service_by_owner=self._service_by_owner,
            spec_by_owner=self._spec_by_owner,
            handle_resolver=self.handle_for,
            exec_in_container=self._exec_in_container,
            status_resolver=self.status,
        )
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
        # Pre-R1 dry-run readiness probe. Handler runs one short claude
        # invocation with a prompt asking the agent to call
        # `mcp__cyberarena__report_ready`. GameMgr watches the bus for
        # AGENT_READY per team within `dry_run_all_agents`. Signature:
        #   async def handler(team_id) -> None
        # None = event-only mode (tests).
        self._dry_run_handler: (
            Callable[[str], Awaitable[None]] | None
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
        # Defense-phase early-end tracking. On every defense kickoff we add
        # (team_id, service) to the set for that round; on_deploy_complete
        # drains it; when the set is empty the round loop is signalled to
        # cut DEFENSE short without waiting for the timer.
        self._defense_pending: dict[int, set[tuple[str, str]]] = {}
        # Per-team AGENT_READY gating for dry-run. Populated by
        # dry_run_all_agents at start; set by record_agent_ready when MCP
        # report_ready arrives. Replaces the old transient bus subscriber.
        self._ready_events: dict[str, asyncio.Event] = {}
        # HealthChecker + HealthPoller are constructed lazily in start()
        # because they bind to the timeline and need the running event
        # loop for their tasks.
        self.health_checker: HealthChecker | None = None
        self.health_poller: HealthPoller | None = None

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

    # ── flag mechanics — thin delegates to FlagServer ──────────────────

    @property
    def flags(self):
        """Backward-compat access to the FlagStore. Callers that need to
        introspect / mutate state directly (tests, scripts) go through
        `game.flags.planted` / `.submissions` / `.plant()` etc. The
        real owner is `self.flag_server.store`."""
        return self.flag_server.store

    def plant_initial_flags(self) -> list[tuple[str, str, str]]:
        return self.flag_server.plant_initial_flags()

    def plant_flags(self, round_id: int) -> list[tuple[str, str, str]]:
        return self.flag_server.plant_flags(round_id=round_id)

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
        events_path = self.run_dir / "events.jsonl"
        self.timeline.attach_file_sink(events_path)
        # Artifact persistence under the same run_dir as events.jsonl.
        # Each ArtifactKind gets a reserved subdir (created on construction);
        # api-logs / agent-logs / service-logs get filled as the round loop
        # runs. See gamemgr/artifacts.py for the layout.
        self.artifacts = ArtifactStore(self.run_dir)
        log.info("game start; events -> %s ; artifacts -> %s", events_path, self.artifacts.root)

        self.emit_setup_events()

        # Persistent-memory v1: one-shot install across all teams BEFORE
        # the round loop ticks. Parallel via asyncio.gather so a 10s npm
        # install doesn't serialize 2x across teams. No-op if no install
        # handler was registered (tests / observer-only runs).
        await self.install_all_agents()

        # Pre-R1 dry-run readiness probe: spawn a tiny agent run per
        # team asking it to call `report_ready` via MCP. Per-team
        # asyncio.Event gates the wait; DRY_RUN_OK / FAILED recorded
        # per outcome. Best-effort — failures don't halt the round loop.
        await self.dry_run_all_agents()

        # Build HealthChecker + HealthPoller now that the event loop is
        # running. The poller doesn't start any tasks until enter_attack_phase.
        self._build_health_facility()

        # RoundLoop calls back into GameMgr at each phase / round boundary
        # (enter_defense_phase, enter_attack_phase, on_round_start,
        # on_round_stop, on_game_end) so dispatch happens via methods, not
        # event subscriptions.
        self.round_loop = RoundLoop(self, self.orchestrator.network, self.config.rounds)
        await self.round_loop.run()

    def _build_health_facility(self) -> None:
        """Construct HealthChecker + HealthPoller. Pulled out of start()
        so tests can build them against an in-memory GameMgr without
        driving the full round loop."""
        self.health_checker = HealthChecker(
            self.timeline,
            exec_in_container=self._exec_in_container,
            service_resolver=lambda team_id, service: self._service_by_owner.get((team_id, service)),
            handle_resolver=self.handle_for,
            status_resolver=self.status,
        )
        teams_by_id = {t.id: t for t in self.config.topology.teams}
        targets = build_targets(
            challenges=self.config.challenges,
            teams_by_id=teams_by_id,
            resolve_spec=self._resolve_spec,
        )
        self.health_poller = HealthPoller(self.health_checker, targets)

    def emit_setup_events(self) -> None:
        """Record one SERVICE_DEPLOYED per challenge-on-container and one
        FLAG_PLANTED per (team, service) at round_id=0.

        Called from start() during normal game lifecycle, but also exposed
        for demos / panel screenshots that don't drive the round loop —
        without this they'd see an empty event stream even though the
        underlying state (deployed services, planted flags) is populated.
        Flag strings are deliberately omitted from FLAG_PLANTED payloads
        (the timeline is broadcast to public observers)."""
        for service, handles in self._deployed:
            for h in handles:
                self.timeline.record(
                    EventType.SERVICE_DEPLOYED,
                    payload={"service": service.name, "container": h.name, "port": service.port},
                )
        for spec, cnames in self._deployed_file_at_path:
            for cn in cnames:
                self.timeline.record(
                    EventType.SERVICE_DEPLOYED,
                    payload={"service": spec.name, "container": cn, "port": spec.port},
                )
        self.flag_server.emit_planted_setup()

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

    # ── Persistent-memory v1: pre-R1 dry-run readiness probe ────────────

    def register_dry_run_handler(
        self,
        handler: Callable[[str], Awaitable[None]],
    ) -> None:
        """Register the per-team dry-run handler.

        Signature: `async def handler(team_id) -> None`. Handler is
        expected to spawn one short claude invocation whose prompt
        asks the agent to call `mcp__cyberarena__report_ready`.
        GameMgr coordinates the per-team timeout + bus watch.

        Without a handler `dry_run_all_agents` no-ops (test mode).
        """
        self._dry_run_handler = handler

    async def dry_run_all_agents(
        self,
        *,
        timeout_secs: float = 195.0,
    ) -> None:
        """Pre-R1 readiness probe across every team in parallel.

        For each team: record DRY_RUN_START, invoke the registered handler
        (which runs a tiny claude phase asking the agent to call
        `mcp__cyberarena__report_ready`), then check whether the
        per-team `_ready_events` flag was set by `record_agent_ready`.
        If yes, record DRY_RUN_OK; otherwise (timeout / handler raised /
        agent process crashed) record DRY_RUN_FAILED with a brief reason.

        Best-effort: a team that fails dry run still gets to try R1
        (its kickoff will likely also fail with a cleaner AGENT_FAILED).
        We don't halt — we just surface the failure on the timeline so
        the operator sees it on the panel before the round loop ticks.

        `timeout_secs` default is the coordinator-side wait; it should
        be slightly larger than main.py's `DRY_RUN_AGENT_TIMEOUT_SECS`
        (the transport-level kill deadline) so the transport always
        wins the race and claude is pkilled cleanly inside the
        container. 195s = 180s transport + 15s slack.
        """
        if self._dry_run_handler is None:
            return  # event-only mode (tests)

        handler = self._dry_run_handler
        teams = list(self.config.topology.teams)

        # Reset per-team gates so a previous dry-run can't satisfy this one.
        for t in teams:
            self._ready_events[t.id] = asyncio.Event()

        async def _one(team_id: str) -> None:
            self.timeline.record(
                EventType.DRY_RUN_START,
                team_id=team_id,
                payload={"team_id": team_id, "ts": time.time()},
            )
            handler_error: str | None = None
            try:
                await asyncio.wait_for(handler(team_id), timeout=timeout_secs)
            except asyncio.TimeoutError:
                handler_error = f"handler exceeded {timeout_secs}s"
            except Exception as e:
                log.exception("dry_run: handler raised for %s", team_id)
                handler_error = f"{type(e).__name__}: {e}"[:200]

            if self._ready_events[team_id].is_set():
                self.timeline.record(
                    EventType.DRY_RUN_OK,
                    team_id=team_id,
                    payload={"team_id": team_id, "ts": time.time()},
                )
            else:
                reason = handler_error or "AGENT_READY not received"
                self.timeline.record(
                    EventType.DRY_RUN_FAILED,
                    team_id=team_id,
                    payload={"team_id": team_id, "error": reason, "ts": time.time()},
                )

        await asyncio.gather(*(_one(t.id) for t in teams))

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

    def _spawn_defense_kickoff(self, round_id: int) -> list[asyncio.Task]:
        """Per-team-per-service defense kickoff for one DEFENSE entry.

        Replaces the old `_on_defense_kickoff` bus subscriber. Records
        DEFENSE_KICKOFF, populates `_defense_pending` for early-end
        tracking, and spawns per-team handler tasks (fire-and-forget;
        returned for tests to await).

        A challenge is "defense-capable" iff its spec carries a
        restart_cmd in metadata. Toy challenges without that — notes
        today — don't get a defense agent; this keeps the kickoff
        signal honest with respect to what the framework can defend.
        """
        kickoffs: list[tuple[str, str, AgentConfig, bool]] = []
        for chcfg in self.config.challenges:
            spec = self._resolve_spec(chcfg.name)
            if spec is None:
                continue
            if not (spec.metadata or {}).get("restart_cmd"):
                continue  # not defense-capable

            for team_id in chcfg.deploy_on:
                key = (round_id, team_id, chcfg.name)
                if key in self._defense_kickoff_done:
                    continue
                self._defense_kickoff_done.add(key)

                self.timeline.record(
                    EventType.DEFENSE_KICKOFF,
                    team_id=team_id,
                    round_id=round_id,
                    phase=Phase.DEFENSE.value,
                    payload={"service": chcfg.name},
                )
                # Track expected DEPLOY_COMPLETE; on_deploy_complete
                # drains this. When empty for the current round we cut
                # the DEFENSE timer short.
                self._defense_pending.setdefault(round_id, set()).add(
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
                kickoffs.append((team_id, chcfg.name, cfg, is_first))

        if not kickoffs or self._defense_kickoff_handler is None:
            return []

        handler = self._defense_kickoff_handler

        async def _safe(team_id: str, service_name: str, cfg: AgentConfig, is_first: bool) -> None:
            try:
                await handler(team_id, cfg, round_id, "defense", is_first)
            except Exception:
                log.exception(
                    "defense kickoff handler raised for (%s, %s); skipping",
                    team_id, service_name,
                )

        return [
            asyncio.create_task(_safe(t, s, c, f))
            for t, s, c, f in kickoffs
        ]

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

    def _spawn_attack_kickoff(self, round_id: int) -> list[asyncio.Task]:
        """Per-team attack kickoff for one ATTACK entry.

        Replaces the old `_on_attack_kickoff` bus subscriber. Records
        ATTACK_KICKOFF and spawns per-team handler tasks (fire-and-forget;
        returned for tests to await).

        Per team (not per-team-per-service) because the M5.6 attack
        prompt walks every opposing prod from a single install.
        Idempotent per (round_id, team_id) so a re-entry in the same
        round doesn't double-spawn.
        """
        kickoffs: list[tuple[str, AgentConfig, bool]] = []
        for team in self.config.topology.teams:
            key = (round_id, team.id)
            if key in self._attack_kickoff_done:
                continue
            self._attack_kickoff_done.add(key)

            self.timeline.record(
                EventType.ATTACK_KICKOFF,
                team_id=team.id,
                round_id=round_id,
                phase=Phase.ATTACK.value,
                payload={},
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
            kickoffs.append((team.id, cfg, is_first))

        if not kickoffs or self._attack_kickoff_handler is None:
            return []

        handler = self._attack_kickoff_handler

        async def _safe(team_id: str, cfg: AgentConfig, is_first: bool) -> None:
            try:
                await handler(team_id, cfg, round_id, "attack", is_first)
            except Exception:
                log.exception(
                    "attack kickoff handler raised for %s; skipping",
                    team_id,
                )

        return [
            asyncio.create_task(_safe(t, c, f))
            for t, c, f in kickoffs
        ]

    # ── DEFCON: end-of-round service-log snapshots ──────────────────────

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

    # ── DEFCON: defense-phase early-end ─────────────────────────────────

    # Note: end-of-defense health check is now driven by HealthPoller on
    # ATTACK entry (see enter_attack_phase). The legacy
    # `_on_defense_end_health_check` subscriber is removed alongside the
    # event-bus migration.

    # ── RoundLoop dispatch — direct method calls, no pub-sub ────────────

    def on_round_start(self, round_id: int, phase: str) -> None:
        """RoundLoop callback: record ROUND_START. No side effects."""
        self.timeline.record(
            EventType.ROUND_START, round_id=round_id, phase=phase,
        )

    def enter_defense_phase(self, round_id: int) -> list[asyncio.Task]:
        """RoundLoop callback: enter DEFENSE for `round_id`.

        Records PHASE_CHANGE, stops the HealthPoller (it's ATTACK-only),
        rotates flags, then spawns per-team defense kickoff tasks.
        Returns the spawned tasks so tests can await them.
        """
        self.timeline.record(
            EventType.PHASE_CHANGE, round_id=round_id, phase=Phase.DEFENSE.value,
        )
        if self.health_poller is not None:
            self.health_poller.on_attack_exit()
        self.flag_server.rotate(round_id)
        return self._spawn_defense_kickoff(round_id)

    def enter_attack_phase(self, round_id: int) -> list[asyncio.Task]:
        """RoundLoop callback: enter ATTACK for `round_id`.

        Records PHASE_CHANGE, starts the HealthPoller (immediate probe
        per target + periodic loop), then spawns per-team attack
        kickoff tasks.
        """
        self.timeline.record(
            EventType.PHASE_CHANGE, round_id=round_id, phase=Phase.ATTACK.value,
        )
        if self.health_poller is not None:
            self.health_poller.on_attack_enter(round_id)
        return self._spawn_attack_kickoff(round_id)

    async def on_round_stop(self, round_id: int, phase: str) -> None:
        """RoundLoop callback: record ROUND_STOP + persist service logs.

        Async because `_persist_service_logs` does blocking docker IO
        wrapped in `run_in_executor` and the caller awaits this so the
        next round doesn't start before logs are flushed.
        """
        self.timeline.record(
            EventType.ROUND_STOP, round_id=round_id, phase=phase,
        )
        if self.artifacts is not None:
            await self._persist_service_logs(round_id)

    def on_game_end(self, round_id: int, phase: str, payload: dict) -> None:
        """RoundLoop callback: record GAME_END (fired in RoundLoop.run's
        finally block, so the timeline carries the terminal event even
        on exception)."""
        self.timeline.record(
            EventType.GAME_END, round_id=round_id, phase=phase, payload=payload,
        )

    async def stop(self) -> None:
        if self.round_loop is not None:
            self.round_loop.stop()
        if self.health_poller is not None:
            await self.health_poller.stop()
        self.timeline.close()

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
        """Thin delegate to FlagServer.submit. Kept `async` for caller
        compatibility (FastAPI / MCP handlers `await` this); the body is
        sync because FlagServer.submit is sync."""
        return self.flag_server.submit(team_id, flag)

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
        """Host record: install preparation finished (files / env /
        mcp.json / ssh ready), supervisor NOT yet launched.

        Kept `async` for caller-compatibility; body is sync since
        timeline.record is sync.
        """
        self.timeline.record(
            EventType.AGENT_INSTALLED,
            team_id=team_id,
            payload={"team_id": team_id, "ts": time.time()},
        )

    async def record_phase_started(
        self,
        team_id: str,
        round_id: int,
        phase: str,
        prompt: str,
        is_first: bool,
    ) -> None:
        """Bus emit: per-phase agent invocation is about to begin.

        Called by the kickoff handler just before
        `agents.runner.run_phase_via_transport`. Carries the first line
        of the prompt + `is_first` flag so panel observers can show
        whether this is the long briefing or a short continuation cue.
        """
        self.timeline.record(
            EventType.AGENT_PROMPT_SENT,
            team_id=team_id,
            round_id=round_id,
            phase=phase,
            payload={
                "prompt_first_line": (prompt.splitlines()[0] if prompt else ""),
                "is_first": is_first,
            },
        )

    async def record_phase_done(
        self,
        team_id: str,
        round_id: int,
        phase: str,
        prompt: str,
        result,  # agents.transport.CommandResult
        started_at: float,
        ended_at: float,
    ) -> None:
        """Bus emit + artifact persistence for one finished phase.

        Writes three sibling files to the team×round artifact dir:
          * `stream-{phase}.jsonl` — raw CLI stdout (stream-json transcript)
          * `agent-{phase}.log`    — raw CLI stderr
          * `metadata-{phase}.json` — exit_code / timed_out / timings /
            prompt_first_line

        Then emits one of AGENT_COMPLETED / AGENT_TIMEOUT / AGENT_FAILED
        depending on `result.timed_out` and `result.exit_code`.

        Best-effort artifact write: if `self.artifacts` is None (start()
        hasn't run) or any write raises, the event still publishes so
        downstream observers see the outcome.
        """
        from .artifacts import ArtifactKind

        duration_ms = int((ended_at - started_at) * 1000)

        if self.artifacts is not None:
            try:
                base = self.artifacts.path_for(
                    ArtifactKind.API_LOG,
                    team_id=team_id, round_id=round_id,
                    filename=f"stream-{phase}.jsonl",
                ).parent
                (base / f"stream-{phase}.jsonl").write_bytes(result.stdout)
                (base / f"agent-{phase}.log").write_bytes(result.stderr)
                (base / f"metadata-{phase}.json").write_text(json.dumps({
                    "round_id": round_id,
                    "phase": phase,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_ms": duration_ms,
                    "prompt_first_line": (prompt.splitlines()[0] if prompt else ""),
                }))
            except Exception:
                log.exception(
                    "record_phase_done: artifact write failed for %s round=%d phase=%s",
                    team_id, round_id, phase,
                )

        payload = {
            "exit_code": result.exit_code,
            "duration_ms": duration_ms,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        # pkilled_at: when the phase boundary actually hit this agent
        # (SIGTERM was issued). The subprocess can take tens of seconds
        # to settle through docker exec; the event ts (recorded just
        # below) is when that settling completed, which can lag the
        # next phase's events on the panel. Surfacing pkilled_at lets
        # the panel render this event at the right relative position
        # without changing the canonical event ts.
        if result.pkilled_at is not None:
            payload["pkilled_at"] = result.pkilled_at
        if result.timed_out:
            event_type = EventType.AGENT_TIMEOUT
        elif result.exit_code == 0:
            event_type = EventType.AGENT_COMPLETED
        else:
            event_type = EventType.AGENT_FAILED
            payload["error_excerpt"] = f"non-zero exit {result.exit_code}"

        self.timeline.record(
            event_type,
            team_id=team_id, round_id=round_id, phase=phase,
            payload=payload,
        )

    async def record_agent_ready(self, team_id: str) -> None:
        """MCP-driven: LLM successfully called `report_ready`. Records to
        timeline and trips the per-team gate used by dry_run_all_agents.
        Best-effort — never load-bearing for the round loop itself."""
        self.timeline.record(
            EventType.AGENT_READY,
            team_id=team_id,
            payload={"team_id": team_id, "ts": time.time()},
        )
        ev = self._ready_events.get(team_id)
        if ev is not None:
            ev.set()

    async def record_deploy_complete(
        self, team_id: str, service: str, duration_ms: int | None
    ) -> None:
        """MCP-driven: agent finished its defense deploy.

        Overwrite-per-(team, service) in `deploy_reports`, record
        DEPLOY_COMPLETE on the timeline, then drain `_defense_pending`
        for the current round. When the set is empty we signal the
        round loop to cut DEFENSE short — replaces the legacy
        `_on_deploy_complete` bus subscriber.

        Caller must have validated `service` via `is_known_service()`.
        """
        completed_at = time.time()
        self.deploy_reports.setdefault(team_id, {})[service] = {
            "duration_ms": duration_ms,
            "completed_at": completed_at,
        }
        st = self.status()
        round_id = st.get("round", 0)
        self.timeline.record(
            EventType.DEPLOY_COMPLETE,
            team_id=team_id,
            round_id=round_id,
            phase=st.get("phase", ""),
            payload={
                "team_id": team_id,
                "service": service,
                "duration_ms": duration_ms,
                "completed_at": completed_at,
            },
        )

        # Drain defense-pending; signal DEFENSE early-end when set empties.
        # ONLY effective during the DEFENSE phase. A late DEPLOY_COMPLETE
        # arriving after ATTACK has started (e.g. a slow agent's
        # report_deploy_complete that flushed past the phase boundary)
        # must NOT touch pending or signal end_phase_early — the
        # early-end signal is phase-agnostic and would otherwise cut
        # ATTACK short. The report is still recorded above for the
        # operator/panel; only the round-loop coupling is gated.
        if st.get("phase") != Phase.DEFENSE.value:
            return
        pending = self._defense_pending.get(round_id)
        if pending is None:
            return  # no defense kickoff was recorded for this round
        pending.discard((team_id, service))
        if not pending and self.round_loop is not None:
            log.info(
                "all defense-capable teams reported DEPLOY_COMPLETE for round %d; "
                "ending DEFENSE phase early",
                round_id,
            )
            self.round_loop.end_phase_early()

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
        self.timeline.record(
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
                self.timeline.record(
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
                self._restart_jobs[job_id]["done"] = True
                return

            self.timeline.record(
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

            # Health check chain — delegated to HealthPoller.trigger_now,
            # which runs the same 4-level probe via HealthChecker.
            if self.health_poller is not None:
                await self.health_poller.trigger_now(team_id, service)
        except Exception as e:
            duration_ms = int((time.time() - started_at) * 1000)
            self.timeline.record(
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

    # Note: the 4-level health-chain primitive lives in
    # `health.checker.HealthChecker.run_chain`. GameMgr no longer owns
    # it — restart-post probe goes through `self.health_poller.trigger_now`
    # and ATTACK-phase periodic polling goes through `self.health_poller`.

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