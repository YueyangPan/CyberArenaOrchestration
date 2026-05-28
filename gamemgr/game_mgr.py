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
from challenges.source import ChallengeSpec, HealthLevel
from models.agent import AgentConfig
from typing import Awaitable, Callable

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
        # DEFCON Phase 4b: registered handler invoked once per (team, service)
        # whenever PHASE_CHANGE → DEFENSE fires. Receives the team_id and the
        # defense-mode AgentConfig; the handler owns the actual ClaudeAgentManager
        # call. None = no-op (the kickoff event still publishes; tests that
        # don't need an agent run without one). Async callable.
        self._defense_kickoff_handler: (
            Callable[[str, AgentConfig], Awaitable[None]] | None
        ) = None
        # Set of (team_id, service_name) pairs we've already kicked off this
        # phase, so a re-fire of PHASE_CHANGE for the same round doesn't
        # spawn duplicate agents.
        self._defense_kickoff_done: set[tuple[int, str, str]] = set()
        # Subscribe at construction time (not start()): the subscriber has
        # no file-sink dependency and tests routinely drive PHASE_CHANGE
        # events directly onto the bus without invoking the round loop.
        self.bus.subscribe(self._on_defense_kickoff)

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

    # ── DEFCON: defense kickoff on PHASE_CHANGE → DEFENSE ───────────────

    def register_defense_kickoff_handler(
        self,
        handler: Callable[[str, AgentConfig], Awaitable[None]],
    ) -> None:
        """Register the callable that actually spawns a defense agent.

        Signature: `async def handler(team_id: str, cfg: AgentConfig)`.
        Called once per (team, deployable defense challenge) on every
        DEFENSE phase entry, after flags rotate.

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

        for chcfg in self.config.challenges:
            # Resolve the spec for this challenge — needed for restart_cmd
            # gating and for the prompt's port + source_dir.
            spec = self._resolve_spec(chcfg.name)
            if spec is None:
                # Unknown / unresolvable spec — skip silently; misconfig is
                # caught earlier in deploy_challenges.
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

                if self._defense_kickoff_handler is None:
                    continue  # signal-only mode (no agent spawn wired)

                try:
                    cfg = self.defense_agent_config_for(team_id, service_name=chcfg.name)
                except Exception as e:
                    log.exception(
                        "defense_agent_config_for failed for (%s, %s): %s",
                        team_id, chcfg.name, e,
                    )
                    continue

                try:
                    await self._defense_kickoff_handler(team_id, cfg)
                except Exception:
                    # A blowing-up handler must not crash the round loop.
                    log.exception(
                        "defense kickoff handler raised for (%s, %s); skipping",
                        team_id, chcfg.name,
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
            handle = self._handle_for(container_name)
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

    def defense_agent_config_for(
        self,
        team_id: str,
        *,
        service_name: str,
        submit_base_url: str = "http://host.docker.internal:8000",
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
            ),
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _handle_for(self, container_name: str) -> ContainerHandle:
        node = self.config.topology.node(container_name)
        c = self.orchestrator.containers._client.containers.get(container_name)
        return ContainerHandle(container_id=c.id, name=container_name, networks=list(node.networks))