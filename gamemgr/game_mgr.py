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

from orchestrator.orchestrator import Orchestrator
from orchestrator.handle import ContainerHandle

from agents.prompts import build_async_prompt, build_attack_prompt, build_defense_prompt
from challenges.interface import Challenge, VulboxTarget
from challenges.registry import build_challenge, health_interval_secs, initial_start
from models.agent import AgentConfig
from typing import Awaitable, Callable

from .artifacts import ArtifactKind, ArtifactStore

# Kickoff handler signature. main.py registers one per phase via
# register_defense_kickoff_handler / register_attack_kickoff_handler;
# the handler awaits `mgr.run_phase(...)` and emits AGENT_COMPLETED /
# TIMEOUT / FAILED. args: (team_id, cfg, round_id, phase_lower, is_first)
PhaseKickoffHandler = Callable[[str, AgentConfig, int, str, bool], Awaitable[None]]

from .async_loop import AsyncLoop
from .config import ChallengeConfig, GameConfig
from .events import EventType, new_event, new_run_dir
from .round import Phase, RoundLoop
from .timeline import Timeline

from facility import FacilityContext, LocalEventSink, LocalExecPort, LocalStatusPort
from flag import FlagServer, SubmitResult
from health.checker import HealthChecker, leaves
from health.poller import HealthPoller, build_targets

log = logging.getLogger(__name__)


class GameMgr:
    # Max time stop() waits for in-flight kickoff agent tasks to settle
    # (write their transcript + emit their terminal AGENT_* event) before
    # closing the timeline. Bounded so a wedged agent can't hang teardown.
    _STOP_DRAIN_GRACE_SECS = 30.0

    def __init__(self, config: GameConfig, orchestrator: Orchestrator) -> None:
        self.config = config
        self.orchestrator = orchestrator
        # Timeline is observation-only: FileSink + WebSocket consume;
        # nothing internal subscribes. Internal control flow goes
        # through direct method calls on GameMgr.
        self.timeline = Timeline()
        # The active game driver: RoundLoop (sync) or AsyncLoop (async).
        # status()/stop() read .phase/.round/.stop()/.end_phase_early() off
        # whichever is active, so they don't branch on mode.
        self.round_loop: RoundLoop | AsyncLoop | None = None
        self.run_dir: Path | None = None
        # (team_id, service_name) -> Challenge instance. Populated by
        # deploy_challenges, read by FlagServer / HealthChecker /
        # restart_service / emit_setup_events. Mutable dict passed by
        # reference so the facilities see deploy mutations.
        self._challenge_by_owner: dict[tuple[str, str], Challenge] = {}
        # FacilityContext — the boundary each facility talks through
        # (status / events / exec). In-process today via Local* adapters;
        # swappable for HTTP adapters once a facility is containerized.
        # `exec` resolves (team_id, role) -> container host-side, so
        # facilities address targets by identity, never by raw name.
        self._facility_ctx = FacilityContext(
            status=LocalStatusPort(self.status),
            events=LocalEventSink(self.timeline),
            exec=LocalExecPort(
                container_resolver=self._resolve_container,
                # Late-bound (not `self._exec_in_container` directly) so a
                # test patching GameMgr._exec_in_container after construction
                # still takes effect.
                docker_exec=lambda container, cmd: self._exec_in_container(container, cmd),
            ),
        )
        # FlagServer owns plant / rotation / submission; GameMgr keeps
        # the public method shapes and delegates.
        self.flag_server = FlagServer(
            self._facility_ctx,
            challenge_by_owner=self._challenge_by_owner,
            target_factory=self.target_for,
        )
        # token -> team_id, populated from config; lookup for API auth.
        self._token_to_team: dict[str, str] = {
            t.token: t.id for t in config.topology.teams if t.token
        }
        # Agent self-reports via MCP. Overwrite-per-(team, service);
        # only the latest deploy report survives.
        self.deploy_reports: dict[str, dict[str, dict]] = {}
        # Service-name whitelist built from yaml — we never compare
        # against a hardcoded literal.
        self._known_services: frozenset[str] = frozenset(
            c.name for c in config.challenges
        )
        # Kickoff handlers run a phase via mgr.run_phase. Signature:
        #   async def handler(team_id, cfg, round_id, phase, is_first)
        # `is_first=True` on the team's first kickoff so the handler
        # picks the long briefing prompt; later kickoffs get a short
        # cue. `None` = event-only mode (used by tests).
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
        # `mcp__cyberarena__report_ready`. GameMgr waits for AGENT_READY
        # per team within `dry_run_all_agents`. Signature:
        #   async def handler(team_id) -> None
        # None = event-only mode (tests).
        self._dry_run_handler: (
            Callable[[str], Awaitable[None]] | None
        ) = None
        # Idempotency keys per round so a re-fire of PHASE_CHANGE doesn't
        # spawn duplicate work.
        self._defense_kickoff_done: set[tuple[int, str, str]] = set()
        # Per (round_id, team_id, service) timestamp of DEFENSE_KICKOFF.
        # `record_deploy_complete` reads this to compute duration_ms
        # server-side — the agent's self-reported value over MCP was
        # unreliable (observed dur=196ms / 175ms in live runs where the
        # agent had actually worked for tens of seconds).
        self._defense_kickoff_ts: dict[tuple[int, str, str], float] = {}
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
        # dry_run_all_agents at start; set by record_agent_ready when
        # the agent's MCP report_ready arrives.
        self._ready_events: dict[str, asyncio.Event] = {}
        # HealthChecker + HealthPoller are constructed lazily in start()
        # because they bind to the running event loop.
        self.health_checker: HealthChecker | None = None
        self.health_poller: HealthPoller | None = None
        # In-flight per-phase agent kickoff tasks (spawned fire-and-forget
        # by _spawn_*_kickoff). Tracked so stop() can drain them before the
        # timeline closes — otherwise the final round's attack agents, which
        # are usually still wrapping up when the loop ends, lose their
        # transcript + terminal AGENT_* event to the shutdown.
        self._kickoff_tasks: set[asyncio.Task] = set()
        # ── async mode (round_config.mode == "async") ───────────────────
        # Set by the active driver (AsyncLoop) when the game ends; the
        # per-team agent supervisors loop until this is set, then stop
        # respawning. Stays clear in sync mode.
        self._game_over: asyncio.Event = asyncio.Event()
        # Long-lived per-team agent supervisor body, registered from main.py
        # (the agent transport lives there, not in GameMgr). Signature:
        #   async def handler(team_id, cfg) -> None
        self._async_supervisor_handler: (
            Callable[[str, AgentConfig], Awaitable[None]] | None
        ) = None
        # Best-effort kill of every live agent process, registered from
        # main.py. Called by stop() in async mode BEFORE draining so the
        # in-flight run_phase returns instead of blocking the drain grace.
        self._kill_all_agents: Callable[[], Awaitable[None]] | None = None
        # Wall-clock start of the current async flag-rotation tick; status()
        # uses it to report seconds_to_rotation. None until the loop ticks.
        self._async_round_started_ts: float | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    def deploy_challenges(self) -> None:
        """Install + first-start every challenge on its target containers.
        Runs synchronously before the round loop starts.

        v2 deploy is uniform across challenges:
          1. Instantiate the Challenge via the registry.
          2. Build a privileged VulboxTarget per team via `target_for`.
          3. Invoke `registry.initial_start` — per-challenge dispatch
             that handles the once-per-game setup (notes ships its
             python daemon + spawns; ico kicks /arena/restart.sh;
             image-baked challenges with a self-starting entrypoint
             leave the hook unregistered for a no-op).
          4. Register (team, service) -> Challenge in
             `_challenge_by_owner` so FlagServer / HealthChecker /
             restart_service can dispatch later.
        """
        for chcfg in self.config.challenges:
            challenge = build_challenge(chcfg.name)
            for team_id in chcfg.deploy_on:
                target = self.target_for(team_id, challenge)
                log.info(
                    "deploying challenge %s -> team=%s host=%s port=%d",
                    chcfg.name, team_id, target.host, chcfg.port,
                )
                initial_start(challenge, target, self._exec_in_container)
                self._challenge_by_owner[(team_id, chcfg.name)] = challenge

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
        """No-op in v2: containers come down with `orchestrator.teardown()`
        which removes the prod images entirely; per-challenge stop hooks
        aren't needed. Kept as a method for symmetry with deploy_challenges
        and to give future challenges (e.g. ones writing to a shared volume)
        a place to clean up if needed."""
        self._challenge_by_owner.clear()

    # ── round loop ──────────────────────────────────────────────────────

    def begin_run(self, run_dir: Path | None = None) -> Path:
        """Open the run: create run_dir, attach the timeline file sink +
        artifact store, and emit SETUP_STARTED.

        Called by main._run_start BEFORE the (blocking) arena bring-up so
        the whole bring-up is recorded + persisted and the live panel can
        stream it. Idempotent: a second call (or start()'s fallback) is a
        no-op once run_dir is set."""
        if self.run_dir is not None:
            return self.run_dir
        self.run_dir = run_dir or new_run_dir()
        events_path = self.run_dir / "events.jsonl"
        self.timeline.attach_file_sink(events_path)
        # Artifact persistence under the same run_dir as events.jsonl.
        self.artifacts = ArtifactStore(self.run_dir)
        log.info("run begin; events -> %s ; artifacts -> %s", events_path, self.artifacts.root)
        self.timeline.record(EventType.SETUP_STARTED, payload={"run": self.run_dir.name})
        return self.run_dir

    # ── arena bring-up telemetry (emitted from main._run_start) ─────────
    # These run on the event-loop thread (Timeline.record is not
    # thread-safe); main does the blocking work in an executor and calls
    # these afterwards.

    def record_image_built(self, tag: str) -> None:
        self.timeline.record(EventType.IMAGE_BUILT, payload={"tag": tag})

    def record_container_ready(self, node) -> None:
        # role is parsed from the node-id suffix (topology convention
        # team{N}_dev / team{N}_prod) so consumers don't have to re-parse.
        nid = node.id
        role = "dev" if nid.endswith("_dev") else "prod" if nid.endswith("_prod") else ""
        self.timeline.record(
            EventType.CONTAINER_READY,
            payload={"node": nid, "role": role, "image": node.image},
        )

    def record_facilities_ready(self) -> None:
        """One FACILITY_READY per constructed facility. flag_server exists
        from __init__; the health poller exists once _build_health_facility
        has run."""
        facilities = [self.flag_server]
        if self.health_poller is not None:
            facilities.append(self.health_poller)
        for f in facilities:
            self.timeline.record(EventType.FACILITY_READY, payload={"facility": f.name})

    def record_setup_complete(self) -> None:
        """SETUP_COMPLETE with a summary of what came up, so the event says
        WHAT finished (containers / deployed services / facilities), not
        just 'done'."""
        facilities = [self.flag_server.name]
        if self.health_poller is not None:
            facilities.append(self.health_poller.name)
        self.timeline.record(
            EventType.SETUP_COMPLETE,
            payload={
                "containers": len(self.config.topology.nodes),
                "services": len(self._challenge_by_owner),
                "facilities": facilities,
            },
        )

    async def start(self, run_dir: Path | None = None) -> None:
        # The setup phase (begin_run + emit_setup_events + facility build)
        # is normally driven by the caller BEFORE start() so the bring-up
        # is recorded live. If it wasn't (standalone / test callers), do
        # the prelude here so start() still works on its own.
        if self.run_dir is None:
            self.begin_run(run_dir)
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

        # Built during the setup phase normally; fall back here.
        if self.health_poller is None:
            self._build_health_facility()

        # Pick the driver by mode. Sync: RoundLoop's DEFENSE↔ATTACK state
        # machine (fresh agent per phase, network toggled per phase). Async:
        # AsyncLoop's single LIVE game (one long-lived supervised agent per
        # team, networks always-on, round tick only rotates flags). Both
        # call back into GameMgr via the same method vocabulary; the sync
        # arm is byte-identical to before.
        if self.config.rounds.mode == "async":
            self.round_loop = AsyncLoop(self, self.orchestrator.network, self.config.rounds)
        else:
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
        self.health_checker = HealthChecker(self._facility_ctx)
        teams_by_id = {t.id: t for t in self.config.topology.teams}
        targets = build_targets(
            challenges=self.config.challenges,
            teams_by_id=teams_by_id,
            challenge_by_name=build_challenge,
            target_factory=self.target_for,
            interval_lookup=health_interval_secs,
        )
        self.health_poller = HealthPoller(self.health_checker, targets)

    def emit_setup_events(self) -> None:
        """Record one SERVICE_DEPLOYED per (team, service) deployment and
        one FLAG_PLANTED per planted (team, service) at round_id=0.

        Called from start() during normal game lifecycle, but also exposed
        for demos / panel screenshots that don't drive the round loop —
        without this they'd see an empty event stream even though the
        underlying state (deployed services, planted flags) is populated.
        Flag strings are deliberately omitted from FLAG_PLANTED payloads
        (the timeline is broadcast to public observers)."""
        team_to_prod = {t.id: t.prod for t in self.config.topology.teams}
        for (team_id, service_name), challenge in self._challenge_by_owner.items():
            self.timeline.record(
                EventType.SERVICE_DEPLOYED,
                team_id=team_id,
                payload={
                    "service": service_name,
                    "container": team_to_prod.get(team_id, ""),
                    "port": next(iter(challenge.vulbox.exposed_ports.values()), 0),
                },
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
        kickoff will record a clean AGENT_FAILED.
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

        Records DEFENSE_KICKOFF, populates `_defense_pending` for the
        early-end tracker, and spawns per-team handler tasks
        (fire-and-forget; returned for tests to await).

        A challenge is "defense-capable" iff its Challenge subclass
        exposes a `restart_handler` (not None). Attack-only challenges
        like notes leave it None; we skip them here so kickoff signals
        only fire for things the framework can actually defend.
        """
        kickoffs: list[tuple[str, str, AgentConfig, bool]] = []
        for chcfg in self.config.challenges:
            challenge = build_challenge(chcfg.name)
            if challenge.restart_handler is None:
                continue

            for team_id in chcfg.deploy_on:
                key = (round_id, team_id, chcfg.name)
                if key in self._defense_kickoff_done:
                    continue
                self._defense_kickoff_done.add(key)
                self._defense_kickoff_ts[key] = time.time()

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

        tasks = [asyncio.create_task(_safe(t, s, c, f)) for t, s, c, f in kickoffs]
        self._track_kickoffs(tasks)
        return tasks

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

        Records ATTACK_KICKOFF and spawns per-team handler tasks
        (fire-and-forget; returned for tests to await). One kickoff per
        team rather than per-service because the attack agent's prompt
        walks every opposing prod from a single invocation. Idempotent
        per (round_id, team_id).
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

        tasks = [asyncio.create_task(_safe(t, c, f)) for t, c, f in kickoffs]
        self._track_kickoffs(tasks)
        return tasks

    # ── async mode: one long-lived supervised agent per team ────────────

    def register_async_supervisor_handler(
        self,
        handler: Callable[[str, AgentConfig], Awaitable[None]],
    ) -> None:
        """Register the per-team long-lived agent supervisor (async mode).

        Signature: `async def handler(team_id, cfg) -> None`. The body
        prompts the agent ONCE (combined attack+defense briefing carried in
        `cfg.system_prompt`) and respawns it with `--continue` if it exits
        before game-over, looping until `is_game_over()`. Kept external (in
        main.py) for the same reason as the phase kickoff handlers — GameMgr
        doesn't own the agent transport. `None` = event-only mode (tests)."""
        self._async_supervisor_handler = handler

    def register_kill_all_agents(
        self,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        """Register a best-effort 'kill every live agent process' callback
        (async mode). `stop()` calls it BEFORE draining so the in-flight
        long-lived run_phase returns instead of blocking the drain grace."""
        self._kill_all_agents = handler

    def start_async_agents(self) -> list[asyncio.Task]:
        """Fire one long-lived supervised agent per team. Called once by
        AsyncLoop at game start. Returns the tasks so tests can await them.

        No-op (returns []) if no supervisor handler was registered
        (event-only mode for tests that drive AsyncLoop without real agents).
        """
        if self._async_supervisor_handler is None:
            return []
        handler = self._async_supervisor_handler

        async def _safe(team_id: str, cfg: AgentConfig) -> None:
            try:
                await handler(team_id, cfg)
            except Exception:
                log.exception("async supervisor handler raised for %s", team_id)

        tasks: list[asyncio.Task] = []
        for team in self.config.topology.teams:
            try:
                cfg = self.async_agent_config_for(team.id)
            except Exception:
                log.exception(
                    "async_agent_config_for failed for %s; skipping", team.id,
                )
                continue
            self._agent_briefed.add(team.id)
            tasks.append(asyncio.create_task(_safe(team.id, cfg)))

        self._track_kickoffs(tasks)
        return tasks

    def is_game_over(self) -> bool:
        """True once the active driver has ended the game. The async agent
        supervisors poll this to stop respawning."""
        return self._game_over.is_set()

    async def record_async_chunk_started(
        self, team_id: str, chunk_n: int, prompt: str,
    ) -> None:
        """AGENT_PROMPT_SENT for one async agent invocation ("chunk"). chunk 0
        is the full combined briefing; later chunks are the resume nudge."""
        round_id = self.round_loop.round if self.round_loop is not None else 0
        self.timeline.record(
            EventType.AGENT_PROMPT_SENT,
            team_id=team_id,
            round_id=round_id,
            phase=Phase.LIVE.value,
            payload={
                "chunk": chunk_n,
                "is_first": chunk_n == 0,
                "prompt_first_line": (prompt.splitlines()[0] if prompt else ""),
            },
        )

    async def record_async_chunk_done(
        self,
        team_id: str,
        chunk_n: int,
        result,  # agents.transport.CommandResult
        started_at: float,
        ended_at: float,
    ) -> None:
        """Artifact persistence + terminal AGENT_* event for one finished
        async chunk. Mirrors `record_phase_done` but writes chunk-numbered
        files into the team's `round-0` dir (async has no per-round phase
        split) and tags events with `phase=LIVE`."""
        from .artifacts import ArtifactKind

        duration_ms = int((ended_at - started_at) * 1000)

        if self.artifacts is not None:
            try:
                base = self.artifacts.path_for(
                    ArtifactKind.API_LOG,
                    team_id=team_id, round_id=0,
                    filename=f"stream-async-{chunk_n}.jsonl",
                ).parent
                (base / f"stream-async-{chunk_n}.jsonl").write_bytes(result.stdout)
                (base / f"agent-async-{chunk_n}.log").write_bytes(
                    result.stdout + result.stderr
                )
                (base / f"metadata-async-{chunk_n}.json").write_text(json.dumps({
                    "chunk": chunk_n,
                    "phase": Phase.LIVE.value,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_ms": duration_ms,
                }))
            except Exception:
                log.exception(
                    "record_async_chunk_done: artifact write failed for %s chunk=%d",
                    team_id, chunk_n,
                )

        round_id = self.round_loop.round if self.round_loop is not None else 0
        payload = {
            "chunk": chunk_n,
            "exit_code": result.exit_code,
            "duration_ms": duration_ms,
            "started_at": started_at,
            "ended_at": ended_at,
        }
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
            team_id=team_id, round_id=round_id, phase=Phase.LIVE.value,
            payload=payload,
        )

    # ── DEFCON: end-of-round service-log snapshots ──────────────────────

    async def _persist_service_logs(self, round_id: int) -> None:
        """Snapshot `docker logs <team{N}_prod>` for every team. End-of-round
        only — captures the prod container's stdout/stderr across DEFENSE +
        ATTACK in one file (timestamps from docker preserve sub-phase ordering).
        """
        if self.artifacts is None:
            return
        client = self.orchestrator.containers._client
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
        """RoundLoop callback: record ROUND_START. Carries total_rounds so
        the panel header can show `round N / total` from round 1 (the only
        other carrier was GAME_END, which is too late)."""
        self.timeline.record(
            EventType.ROUND_START,
            round_id=round_id,
            phase=phase,
            payload={"total_rounds": self.config.rounds.total_rounds},
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

    def _track_kickoffs(self, tasks: list[asyncio.Task]) -> None:
        """Register fire-and-forget kickoff tasks so stop() can drain them.
        The done-callback keeps the set from growing across rounds."""
        for t in tasks:
            self._kickoff_tasks.add(t)
            t.add_done_callback(self._kickoff_tasks.discard)

    async def _drain_kickoffs(self) -> None:
        """Wait (bounded) for in-flight kickoff tasks to settle, so their
        record_phase_done — transcript write + terminal AGENT_* event —
        completes before the timeline closes. A straggler past the grace
        is cancelled rather than hanging teardown.

        Async-mode safety net: a straggler usually means the agent's
        `docker exec` is still blocked because the in-container `claude`
        wasn't reaped. Cancelling the task then would orphan that process,
        so before cancelling we re-invoke `_kill_all_agents` (hard SIGKILL)
        and re-wait — a real kill makes `run_command` return on its own, so
        cancel becomes a true last resort. Sync mode has no `_kill_all_agents`
        (None) and falls straight through to cancel — unchanged."""
        pending = [t for t in self._kickoff_tasks if not t.done()]
        if not pending:
            return
        log.info("stop: draining %d in-flight kickoff task(s)", len(pending))
        _done, still = await asyncio.wait(pending, timeout=self._STOP_DRAIN_GRACE_SECS)
        if still and self._kill_all_agents is not None:
            log.warning(
                "stop: %d task(s) still pending after %.0fs; re-killing agents "
                "before cancel (avoid orphaning in-container claude)",
                len(still), self._STOP_DRAIN_GRACE_SECS,
            )
            try:
                await self._kill_all_agents()
            except Exception:
                log.exception("stop: re-kill of agents failed")
            _done2, still = await asyncio.wait(still, timeout=self._STOP_DRAIN_GRACE_SECS)
        if still:
            log.warning(
                "stop: %d kickoff task(s) exceeded drain grace; cancelling",
                len(still),
            )
            for t in still:
                t.cancel()

    async def stop(self) -> None:
        if self.round_loop is not None:
            self.round_loop.stop()
        # Async mode: agents are long-lived and the in-flight run_phase can be
        # mid-await on a multi-minute chunk. Cancelling the supervisor task
        # does NOT kill the detached in-container claude, so kill the agent
        # processes explicitly BEFORE draining — otherwise the drain blocks up
        # to the chunk timeout. No-op (None) in sync mode, so sync stop() is
        # unchanged. _game_over is also set so any looping supervisor exits.
        self._game_over.set()
        if self._kill_all_agents is not None:
            try:
                await self._kill_all_agents()
            except Exception:
                log.exception("stop: kill_all_agents failed")
        # Drain in-flight per-phase agent tasks BEFORE closing the timeline,
        # so the final round's transcripts + terminal AGENT_* events are
        # captured (the last attack agents are typically still wrapping up
        # when the round loop ends).
        await self._drain_kickoffs()
        if self.health_poller is not None:
            await self.health_poller.stop()
        self.timeline.close()

    # ── read-only views ─────────────────────────────────────────────────

    def status(self) -> dict:
        total = self.config.rounds.total_rounds
        mode = self.config.rounds.mode
        if self.round_loop is None:
            st = {"phase": Phase.INIT.value, "round": 0, "total_rounds": total}
        else:
            st = {
                "phase": self.round_loop.phase.value,
                "round": self.round_loop.round,
                "total_rounds": total,
            }
        # `mode` is global (not team-specific), safe to expose to any agent.
        st["mode"] = mode
        if mode == "async":
            # Pacing hints for the self-directing async agent: how long the
            # flag-rotation interval is, and roughly how long until the next
            # rotation (flags it stole go stale at rotation).
            st["round_secs"] = self.config.rounds.round_secs
            if self._async_round_started_ts is not None:
                elapsed = time.time() - self._async_round_started_ts
                st["seconds_to_rotation"] = max(
                    0, int(self.config.rounds.round_secs - elapsed)
                )
        return st

    async def submit_flag(self, team_id: str, flag: str) -> SubmitResult:
        """Thin delegate to FlagServer.submit. Kept `async` for caller
        compatibility (FastAPI / MCP handlers `await` this); the body is
        sync because FlagServer.submit is sync."""
        return self.flag_server.submit(team_id, flag)

    # ── MCP lifecycle / report emitters ─────────────────────────────────

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
        `AgentManager.run_phase`. Carries the first line
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
          * `agent-{phase}.log`    — raw combined stdout+stderr (original archive)
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
                # Raw combined capture: full stdout (the stream-json) followed
                # by stderr. stdout/stderr arrive on separate pipes, so this is
                # concatenated (stderr last), not time-interleaved. Redundant
                # with stream-*.jsonl by design — the unparsed original archive.
                (base / f"agent-{phase}.log").write_bytes(result.stdout + result.stderr)
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
        self, team_id: str, service: str
    ) -> None:
        """MCP-driven: agent finished its defense deploy.

        Overwrite-per-(team, service) in `deploy_reports`, record
        DEPLOY_COMPLETE on the timeline, then drain `_defense_pending`
        for the current round (DEFENSE phase only — see phase gate
        below). Caller must have validated `service` via
        `is_known_service()`.

        `duration_ms` is computed server-side as (now - DEFENSE_KICKOFF
        ts for this round/team/service). The agent's self-reported
        value over MCP was unreliable — observed garbage values like
        dur=196ms / 175ms in live runs where the agent had actually
        worked tens of seconds. Anchoring duration to the kickoff
        timestamp the host already controls gives a trustworthy number.
        """
        completed_at = time.time()
        st = self.status()
        round_id = st.get("round", 0)
        kickoff_ts = self._defense_kickoff_ts.get((round_id, team_id, service))
        duration_ms = int((completed_at - kickoff_ts) * 1000) if kickoff_ts is not None else None
        self.deploy_reports.setdefault(team_id, {})[service] = {
            "duration_ms": duration_ms,
            "completed_at": completed_at,
        }
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

    async def restart_service(
        self,
        team_id: str,
        service: str,
    ) -> str:
        """Async restart of `service` on `team_id`'s prod container.

        Returns a `job_id` immediately; the rebuild + restart runs in a
        background task that emits SERVICE_RESTART_STARTED / _COMPLETED /
        _FAILED + a SERVICE_HEALTH_CHECK per level.

        Raises ValueError for unknown service, attack-only challenge
        (no restart_handler), a restart already in flight for this
        (team, service), or a call during the ATTACK phase (restart is a
        DEFENSE-recovery action). The MCP layer translates these into
        InvalidParams.
        """
        # 1. Challenge lookup (v2: restart goes through challenge.restart_handler).
        try:
            challenge = build_challenge(service)
        except KeyError:
            raise ValueError(f"unknown service: {service!r}")
        if challenge.restart_handler is None:
            raise ValueError(
                f"service {service!r} has no restart handler — not defendable"
            )

        # 2. Container lookup. Topology has one team{N}_prod per team.
        team = next((t for t in self.config.topology.teams if t.id == team_id), None)
        if team is None or not team.prod:
            raise ValueError(f"unknown team: {team_id!r}")
        container_name = team.prod

        # 2b. Phase gate — restart is DEFENSE-only. An attack agent's
        # --continue session sometimes finishes a leftover defense restart
        # in the ATTACK phase; reject it (no rebuild) and surface a
        # SERVICE_RESTART_REJECTED so it's visible without inflating the
        # _FAILED count.
        st = self.status()
        if (st.get("phase") or "").upper() == Phase.ATTACK.value:
            self.timeline.record(
                EventType.SERVICE_RESTART_REJECTED,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={"team_id": team_id, "service": service, "reason": "WRONG_PHASE"},
            )
            raise ValueError(
                f"restart_service is DEFENSE-only; current phase is {st.get('phase', '')!r}"
            )

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
                challenge=challenge,
            )
        )
        return job_id

    async def _run_restart_job(
        self,
        *,
        job_id: str,
        team_id: str,
        service: str,
        challenge: Challenge,
    ) -> None:
        """Run challenge.restart_handler.run(target) in a thread executor,
        emit COMPLETED/FAILED with duration_ms based on the returned
        CheckResult, then trigger a post-restart health probe."""
        started_at = self._restart_jobs[job_id]["started_at"]
        st = self.status()
        loop = asyncio.get_running_loop()
        target = self.target_for(team_id, challenge)
        try:
            # RestartHandler.run is blocking by contract (docker exec under
            # the hood); push it off the event loop.
            result = await loop.run_in_executor(
                None, challenge.restart_handler.run, target,
            )
            duration_ms = int((time.time() - started_at) * 1000)
            if not result.passed:
                # Surface the first failed leaf's detail as the error
                # message — operators want to know WHERE compile/spawn/smoke
                # broke without expanding the full tree in the panel.
                failed = next(
                    (leaf for leaf in leaves(result) if not leaf.passed),
                    None,
                )
                error_detail = (
                    f"{failed.name}: {failed.detail}" if failed is not None
                    else (result.detail or "restart failed")
                )
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
                        "error": error_detail[-200:],
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

            # Health probe — delegated to HealthPoller.trigger_now, which
            # runs challenge.functionality_test via HealthChecker.
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
        Called from the thread pool by `_run_restart_job` and on the hot
        plant/health/restart paths. Reuses the orchestrator's persistent
        docker client — instantiating fresh `docker.from_env()` here was
        the source of a 60s `version` probe timeout that killed a live
        run at end-of-game log persistence."""
        c = self.orchestrator.containers._client.containers.get(container_name)
        result = c.exec_run(["bash", "-c", cmd])
        out = result.output.decode(errors="replace") if result.output else ""
        return result.exit_code, out

    # Note: the 4-level health-chain primitive lives in
    # `health.checker.HealthChecker.run_chain`. GameMgr no longer owns
    # it — restart-post probe goes through `self.health_poller.trigger_now`
    # and ATTACK-phase periodic polling goes through `self.health_poller`.

    # ── agent provisioning ──────────────────────────────────────────────

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
        edit→restart→smoke→report recipe via `build_defense_prompt`.
        Pulls port from the Challenge's VulboxImage so the prompt
        points the agent at the right port inside its own prod container.
        """
        base = self.config.agents.get(team_id)
        if base is None:
            raise KeyError(f"no agent config for team {team_id!r} in game.yaml")

        teams_by_id = {t.id: t for t in self.config.topology.teams}
        if team_id not in teams_by_id:
            raise KeyError(f"unknown team_id {team_id!r}")
        own_team = teams_by_id[team_id]

        try:
            challenge = build_challenge(service_name)
        except KeyError:
            raise ValueError(f"unknown service: {service_name!r}")
        port = next(iter(challenge.vulbox.exposed_ports.values()), 0)

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
                port=port,
                is_first=is_first,
            ),
        )

    def async_agent_config_for(
        self,
        team_id: str,
        submit_base_url: str = "http://host.docker.internal:8000",
    ) -> AgentConfig:
        """Build the per-team AgentConfig for ASYNC mode.

        Combines the attack topology (opposing prods, like `agent_config_for`)
        and the defense context (own service + port, like
        `defense_agent_config_for`) into one config whose `system_prompt` is
        the combined attack+defense briefing (`build_async_prompt`). This is
        the agent's only prompt — the supervisor re-sends this same briefing
        verbatim on any restart (no separate resume prompt).
        """
        base = self.config.agents.get(team_id)
        if base is None:
            raise KeyError(f"no agent config for team {team_id!r} in game.yaml")

        teams_by_id = {t.id: t for t in self.config.topology.teams}
        if team_id not in teams_by_id:
            raise KeyError(f"unknown team_id {team_id!r}")
        own_team = teams_by_id[team_id]
        opposing = tuple(t.prod for tid, t in teams_by_id.items() if tid != team_id)

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

        # The single configured challenge for this team drives both halves of
        # the prompt (attack target service + own defendable service + port).
        # A challenge is defendable iff it exposes a restart_handler (attack-
        # only challenges like notes don't) — when it isn't, the defense half
        # collapses to a one-liner so the agent doesn't call restart_service
        # on a service that would reject it.
        service: str | None = None
        port = 0
        defendable = False
        deployable = [c for c in self.config.challenges if team_id in c.deploy_on]
        if len(deployable) == 1:
            service = deployable[0].name
            try:
                challenge = build_challenge(service)
                port = next(iter(challenge.vulbox.exposed_ports.values()), 0)
                defendable = challenge.restart_handler is not None
            except KeyError:
                port = 0
        return replace(
            cfg,
            system_prompt=build_async_prompt(
                cfg, service_name=service, port=port, defendable=defendable,
            ),
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))

    def target_for(self, team_id: str, challenge: Challenge) -> VulboxTarget:
        """Build a privileged VulboxTarget for `team_id`'s deployment of `challenge`.

        Runtime coordinates pulled from topology + the challenge's declared
        vulbox image:
          host  — team's prod container name (the docker exec target)
          ports — copy of challenge.vulbox.exposed_ports (same image, same
                  ports across teams; per-team variation is only in host)
          meta  — conventional payload that probes look for:
                    team_id           — included so probes can log with team
                                        context without re-threading it
                    exec_in_container — the docker-exec callable contract

        This is the PRIVILEGED target — own-team docker-exec access. It's
        what FlagServer.plant uses, what HealthChecker.functionality_test
        uses, and what restart_service hands to RestartHandler. Cross-team
        attack agents build their own VulboxTarget pointing at the victim
        over the public network and don't go through this helper.
        """
        host = next(
            (t.prod for t in self.config.topology.teams if t.id == team_id),
            None,
        )
        if host is None:
            raise KeyError(f"target_for: unknown team_id {team_id!r}")

        # Adapt ExecPort (identity-addressed, ExecResult) down to the
        # challenge-facing `(container_name, cmd) -> (rc, output)` contract
        # documented in challenges/interface/probe.py. The container_name
        # arg is ignored: this is the privileged prod target, so the port
        # resolves (team_id, "prod") to the same host the handler passes.
        exec_port = self._facility_ctx.exec

        def _exec_via_port(_container_name: str, cmd: str) -> tuple[int, str]:
            r = exec_port.exec(team_id, "prod", cmd)
            return r.exit_code, r.stdout

        return VulboxTarget(
            host=host,
            ports=dict(challenge.vulbox.exposed_ports),
            meta={
                "team_id": team_id,
                "exec_in_container": _exec_via_port,
            },
        )

    def _resolve_container(self, team_id: str, role: str = "prod") -> str:
        """Map an exec identity (team_id, role) to a concrete container
        name. Host-owned so the ExecPort never trusts a caller-supplied
        container name (see facility/ports.py::ExecPort)."""
        team = next((t for t in self.config.topology.teams if t.id == team_id), None)
        if team is None:
            raise KeyError(f"_resolve_container: unknown team_id {team_id!r}")
        name = {"prod": team.prod, "dev": team.dev}.get(role)
        if not name:
            raise KeyError(
                f"_resolve_container: no {role!r} container for team {team_id!r}"
            )
        return name