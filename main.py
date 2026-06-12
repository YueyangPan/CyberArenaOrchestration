"""Cyber Arena orchestrator CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
from pathlib import Path

import time

import docker
import uvicorn

from agents.manager import AgentManager
from agents.prompts import ASYNC_RESUME_NUDGE
from agents.registry import build as build_agent_manager
from agents.transport_docker import DockerExecBackend
from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from models.agent import AgentConfig
from orchestrator import topology
from orchestrator.orchestrator import Orchestrator

log = logging.getLogger("main")

DEFAULT_CONFIG = Path(__file__).parent / "config" / "game.yaml"


class _DropAccessPaths(logging.Filter):
    """Drop uvicorn access-log lines for high-frequency poll endpoints (the
    panel's /game/agents transcript poll) to keep the log readable. Other
    requests — MCP tool calls, flag submits — still log normally."""

    def __init__(self, *needles: str) -> None:
        super().__init__()
        self._needles = needles

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(n in msg for n in self._needles)

# Pre-R1 dry-run readiness probe. Sent as the prompt to a tiny one-turn
# claude invocation per team before the round loop starts. Body kept
# minimal: success path is one MCP call. Failure path doesn't need the
# agent to do anything special — if it can't reach MCP, the orchestrator
# observes the missing AGENT_READY and emits DRY_RUN_FAILED on timeout.
_DRY_RUN_PROMPT = (
    "Pre-game setup check. Call the MCP tool "
    "`mcp__cyberarena__report_ready` once, then exit immediately.\n"
    "\n"
    "Do not run any shell commands, edit any files, or perform any other\n"
    "actions. This is purely an MCP-channel readiness probe — the\n"
    "orchestrator infers that your CLI / auth / mcp.json / X-Team-Token\n"
    "are all wired correctly when it sees your report_ready land.\n"
)

# Outer cap on how long the dry-run agent is given to call report_ready.
# Generous to absorb cold-start: first claude invocation after npm install
# can take 30-90s before the model returns the first token, plus any proxy
# / rate-limit hiccups. The first live run hit this exact ceiling on
# team1 (handler exceeded 60.0s), so we matched defense_secs (180s) here.
# GameMgr's `dry_run_all_agents(timeout_secs=...)` is set to this + a
# few seconds of slack so the transport-level timeout fires FIRST (the
# transport kills claude cleanly via pkill; the coordinator timeout
# would only fire if the transport itself hung).
DRY_RUN_AGENT_TIMEOUT_SECS = 180.0

# ── async-mode supervisor tuning ────────────────────────────────────────
# Per-invocation ("chunk") cap on the long-lived async agent. Generous: the
# agent is meant to loop continuously until the game ends, so this is a hang
# safety net (and an upper bound on per-process context growth for very long
# games — a timed-out chunk is respawned with --continue, losing no memory).
# End-of-game teardown does NOT rely on this — stop() kills agents explicitly.
ASYNC_CHUNK_TIMEOUT_SECS = 3600.0
# If a chunk exits faster than this, treat it as an unhealthy/crash exit for
# backoff purposes (a healthy agent runs for minutes). Cold-start can be slow,
# so this is well below a normal first-token latency budget but above an
# instant auth/mcp.json failure.
ASYNC_MIN_HEALTHY_SECS = 20.0
# Give up respawning a team's agent after this many consecutive fast exits, so
# a broken config (bad api_key, unreachable MCP) can't hot-loop the API.
ASYNC_MAX_FAST_EXITS = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cyberarena")
    parser.add_argument("command", choices=["setup", "teardown", "start"])
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "setup":
        # Build every challenge image the config references before
        # container create — otherwise Orchestrator.setup() will try to
        # docker-pull the unpublished local tag and fail.
        _build_challenge_images(args.config)
        graph = topology.load(args.config)
        Orchestrator(graph=graph).setup()
        return 0

    if args.command == "teardown":
        graph = topology.load(args.config)
        Orchestrator(graph=graph).teardown()
        return 0

    if args.command == "start":
        return asyncio.run(_run_start(args))

    return 1


async def _run_start(args) -> int:
    from challenges.registry import build_images

    cfg = load_game_config(args.config)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)

    # Persistent-memory v1: install once pre-loop, run_phase per phase (sync)
    # or one long-lived supervised agent per team (async). Both wire install +
    # dry-run handlers; they differ only in how agents are driven during the
    # game.
    if cfg.rounds.mode == "async":
        _wire_async_supervisor(game)
    else:
        _wire_phase_kickoffs(game)

    # Open the run + bring the API server up BEFORE the arena bring-up so
    # the live panel can connect and stream the bring-up events.
    game.begin_run()
    # Snapshot the raw config into the run dir as a record (teams,
    # challenge, rounds, per-phase seconds). Copied verbatim so the
    # `${VAR}` placeholders stay unexpanded — no secrets land on disk.
    try:
        shutil.copyfile(args.config, game.run_dir / "config.yaml")
    except OSError:
        log.warning("could not snapshot config %s into run dir", args.config)
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    uv_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(uv_config)
    # Keep the panel's recurring transcript poll out of the access log.
    logging.getLogger("uvicorn.access").addFilter(_DropAccessPaths("/game/agents"))
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    # Arena bring-up. The steps are blocking (docker / image builds), so
    # run each in a thread executor to keep the event loop free to serve
    # the panel; emit the per-step events from the loop thread afterwards
    # (Timeline.record is not thread-safe). build_images is idempotent
    # (cached tags skip), so on a warm host this is near-instant.
    loop = asyncio.get_running_loop()
    tags = await loop.run_in_executor(None, build_images, cfg)
    for tag in tags:
        game.record_image_built(tag)
    await loop.run_in_executor(None, orch.setup)
    for node in cfg.topology.nodes:
        game.record_container_ready(node)
    await loop.run_in_executor(None, game.deploy_challenges)
    await loop.run_in_executor(None, game.plant_initial_flags)
    game.emit_setup_events()          # SERVICE_DEPLOYED + FLAG_PLANTED (round 0)
    game._build_health_facility()
    game.record_facilities_ready()    # FACILITY_READY (flag, health)
    game.record_setup_complete()

    loop_task = asyncio.create_task(game.start(), name="round-loop")

    def _signal_handler():
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows fallback; we don't target Windows

    async def _wait_for_shutdown():
        await shutdown.wait()

    done, pending = await asyncio.wait(
        [loop_task, asyncio.create_task(_wait_for_shutdown())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    await game.stop()

    # Snapshot the live panel to runs/<ts>/panel.html so the run is
    # readable offline (open the file in any browser, get the full
    # event timeline rendered as it was on the live panel). Best-
    # effort: a write failure here doesn't block teardown.
    if game.run_dir is not None:
        try:
            from gamemgr.panel_snapshot import write_snapshot
            write_snapshot(game.run_dir)
        except Exception:
            log.exception("failed to write panel snapshot")

    # Signal the long-lived panel WebSocket(s) to close so uvicorn's graceful
    # drain can actually complete. On a natural end-of-game (loop_task done)
    # `shutdown` was never set; setting it now lets /events/stream return
    # instead of blocking on queue.get() forever — without this the drain
    # times out, we force_exit, and uvicorn logs a CancelledError from the
    # cancelled lifespan/MCP task even though the match ended cleanly.
    shutdown.set()

    # Tell uvicorn to wind down. `should_exit` triggers graceful shutdown
    # (drains in-flight requests + closes idle keep-alives). With the WS
    # streams now closing on `shutdown`, this normally completes well within
    # the grace; force_exit below is the belt-and-suspenders for any other
    # long-lived stream (e.g. a lingering MCP SSE session).
    # Note: force_exit makes uvicorn's serve() finish by raising
    # CancelledError, so every wait below must catch CancelledError as well
    # as TimeoutError — otherwise it escapes _run_start and asyncio.run
    # dumps the whole stack on an otherwise-clean shutdown.
    server.should_exit = True
    try:
        await asyncio.wait_for(server_task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        log.warning(
            "uvicorn didn't exit within 10s (a live panel WebSocket / MCP SSE "
            "session is still open); forcing exit"
        )
        server.force_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("uvicorn force_exit didn't settle in 5s; cancelling the server task")
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass

    if not loop_task.done():
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    game.teardown_challenges()
    orch.teardown()
    return 0


def _build_challenge_images(config_path: str) -> None:
    """Build every challenge image referenced by `config_path`.

    Delegates to `challenges.registry.build_images` — the registry owns
    the per-challenge image builder map, so adding a new challenge only
    needs one entry there, not a branch here.

    Idempotent: each builder skips when the tag is already present locally.
    """
    from challenges.registry import build_images

    cfg = load_game_config(config_path)
    tags = build_images(cfg)
    if tags:
        log.info("built/cached %d challenge image(s): %s", len(tags), tags)


def _wire_agent_common(game: GameMgr):
    """Shared agent-layer setup for both modes: one transport
    (DockerExecBackend), one manager per provider (via `agents.registry.build`),
    and the two pre-loop handlers GameMgr.start() always runs —

      * `install_handler` — once per team from `game.install_all_agents()`;
        bridges the sync `AgentManager.install` into the loop via executor.
      * `dry_run_handler` — pre-R1 readiness probe (one report_ready call).

    Returns `(backend, _mgr_for)` so the caller can wire the mode-specific
    agent driver (sync per-phase kickoffs or the async supervisor)."""
    client = docker.from_env()
    backend = DockerExecBackend(client=client)

    # One AgentManager per team — picked from cfg.provider via
    # agents.registry.build. With one provider in play this is a single
    # cached instance; mixed-provider games (claude vs codex) will pick
    # different subclasses per team automatically. Each manager has its
    # own DockerExecBackend internally for install; the shared `backend`
    # above is what the per-phase runner uses.
    mgr_by_team: dict[str, AgentManager] = {}
    def _mgr_for(team_id: str) -> AgentManager:
        if team_id not in mgr_by_team:
            cfg = game.config.agents.get(team_id)
            if cfg is None:
                raise KeyError(f"no agent config for team {team_id!r}")
            mgr_by_team[team_id] = build_agent_manager(cfg)
        return mgr_by_team[team_id]

    # ── install handler (pre-loop) ─────────────────────────────────────
    async def install_handler(team_id: str, cfg: AgentConfig) -> None:
        team = next((t for t in game.config.topology.teams if t.id == team_id), None)
        if team is None or not team.dev:
            log.warning("install: team %s has no dev container; skipping", team_id)
            return
        try:
            handle = game.handle_for(team.dev)
        except Exception:
            log.exception("install: cannot resolve dev handle for %s", team_id)
            return
        mgr = _mgr_for(team_id)
        loop = asyncio.get_running_loop()
        def _on_installed(tid: str) -> None:
            asyncio.run_coroutine_threadsafe(game.record_agent_installed(tid), loop)
        await loop.run_in_executor(
            None,
            lambda: mgr.install(handle, cfg, on_installed=_on_installed),
        )
        log.info("agent installed on %s", team.dev)

    game.register_install_agent_handler(install_handler)

    # ── pre-R1 dry-run handler ─────────────────────────────────────────
    # Tiny one-turn invocation: ask the agent to call report_ready via
    # MCP, then exit. Success is observed by GameMgr.dry_run_all_agents
    # waiting for AGENT_READY per team.
    async def dry_run_handler(team_id: str) -> None:
        team = next((t for t in game.config.topology.teams if t.id == team_id), None)
        if team is None or not team.dev:
            log.warning("dry_run: team %s has no dev container; skipping", team_id)
            return
        try:
            handle = game.handle_for(team.dev)
        except Exception:
            log.exception("dry_run: cannot resolve dev handle for %s", team_id)
            return
        mgr = _mgr_for(team_id)
        cfg = game.config.agents.get(team_id)
        model = (cfg.model if cfg is not None else "") or None
        prompt = _DRY_RUN_PROMPT
        # Run the agent. We don't persist artifacts here (it's a single
        # MCP call). Success/failure is signalled via AGENT_READY,
        # observed by GameMgr.dry_run_all_agents.
        await mgr.run_phase(
            backend, handle, prompt,
            timeout=DRY_RUN_AGENT_TIMEOUT_SECS, model=model,
        )

    game.register_dry_run_handler(dry_run_handler)

    return backend, _mgr_for


def _wire_phase_kickoffs(game: GameMgr) -> None:
    """Sync mode: wire per-phase defense/attack kickoff handlers.

    Each runs per (team, phase) from the kickoff subscribers. The whole body
    is the 5-line dance: lookup handle → record_phase_started → mgr.run_phase
    → record_phase_done. CLI-specific details (prompt path, claude argv, kill
    pattern) live behind `mgr.PROMPT_PATH` / `mgr.phase_command()` /
    `mgr.PHASE_KILL_PATTERN`."""
    backend, _mgr_for = _wire_agent_common(game)

    # ── per-phase kickoff handler ──────────────────────────────────────
    def _make_phase_handler(timeout_secs: float):
        async def handler(
            team_id: str,
            cfg: AgentConfig,
            round_id: int,
            phase: str,
            is_first: bool,
        ) -> None:
            team = next((t for t in game.config.topology.teams if t.id == team_id), None)
            if team is None or not team.dev:
                log.warning("%s kickoff: team %s has no dev container; skipping", phase, team_id)
                return
            try:
                handle = game.handle_for(team.dev)
            except Exception:
                log.exception("%s kickoff: cannot resolve dev handle for %s", phase, team_id)
                return

            mgr = _mgr_for(team_id)
            prompt = cfg.system_prompt or ""
            await game.record_phase_started(team_id, round_id, phase, prompt, is_first)

            started_at = time.time()
            try:
                result = await mgr.run_phase(
                    backend, handle, prompt,
                    timeout=timeout_secs, model=cfg.model or None,
                )
            except Exception as e:
                log.exception("%s kickoff: run_phase raised for %s", phase, team_id)
                from gamemgr.events import EventType
                game.timeline.record(
                    EventType.AGENT_FAILED,
                    team_id=team_id, round_id=round_id, phase=phase,
                    payload={"error": f"{type(e).__name__}: {e}"[:200]},
                )
                return
            ended_at = time.time()

            await game.record_phase_done(
                team_id, round_id, phase, prompt,
                result=result, started_at=started_at, ended_at=ended_at,
            )
        return handler

    game.register_defense_kickoff_handler(
        _make_phase_handler(game.config.rounds.defense_secs)
    )
    game.register_attack_kickoff_handler(
        _make_phase_handler(game.config.rounds.attack_secs)
    )


async def _run_async_agent(
    game: GameMgr,
    backend,
    mgr: AgentManager,
    handle,
    team_id: str,
    cfg: AgentConfig,
) -> None:
    """The long-lived async agent loop for one team.

    Prompts the agent ONCE with the combined briefing (`cfg.system_prompt`),
    then respawns it with the short resume nudge (`--continue` carries the
    briefing in session memory) whenever it exits before the game is over.
    A crash-loop guard (fast-exit backoff + giveup) stops a broken config from
    hot-looping the API. Module-level (not a closure) so it's unit-testable
    with fakes."""
    from gamemgr.events import EventType

    model = cfg.model or None
    prompt = cfg.system_prompt or ""   # chunk 0 = full combined briefing
    n = 0
    fast_exits = 0
    while not game.is_game_over():
        await game.record_async_chunk_started(team_id, n, prompt)
        started_at = time.time()
        try:
            result = await mgr.run_phase(
                backend, handle, prompt,
                timeout=ASYNC_CHUNK_TIMEOUT_SECS, model=model,
            )
        except Exception as e:
            log.exception("async supervisor: run_phase raised for %s (chunk %d)", team_id, n)
            game.timeline.record(
                EventType.AGENT_FAILED,
                team_id=team_id, round_id=0, phase="LIVE",
                payload={"chunk": n, "error": f"{type(e).__name__}: {e}"[:200]},
            )
            result = None
        ended_at = time.time()

        if result is not None:
            await game.record_async_chunk_done(team_id, n, result, started_at, ended_at)

        if game.is_game_over():
            break

        # Crash-loop guard: a healthy agent runs for minutes. Repeated
        # near-instant exits mean a broken config (bad token, MCP unreachable)
        # — back off, and give up before hot-looping the API.
        if (ended_at - started_at) < ASYNC_MIN_HEALTHY_SECS:
            fast_exits += 1
            if fast_exits >= ASYNC_MAX_FAST_EXITS:
                log.warning(
                    "async supervisor: team %s exited fast %d times; giving up",
                    team_id, fast_exits,
                )
                game.timeline.record(
                    EventType.AGENT_FAILED,
                    team_id=team_id, round_id=0, phase="LIVE",
                    payload={"chunk": n, "error": f"gave up after {fast_exits} fast exits"},
                )
                break
            await asyncio.sleep(min(2 ** fast_exits, 30))
        else:
            fast_exits = 0
        n += 1
        prompt = ASYNC_RESUME_NUDGE


def _wire_async_supervisor(game: GameMgr) -> None:
    """Async mode: wire one long-lived supervised agent per team.

    Resolves each team's dev handle + manager, then drives the agent via
    `_run_async_agent`. `kill_all_agents` lets stop() reap live agents so
    end-of-game teardown doesn't block on the chunk timeout."""
    backend, _mgr_for = _wire_agent_common(game)

    async def supervisor(team_id: str, cfg: AgentConfig) -> None:
        team = next((t for t in game.config.topology.teams if t.id == team_id), None)
        if team is None or not team.dev:
            log.warning("async supervisor: team %s has no dev container; skipping", team_id)
            return
        try:
            handle = game.handle_for(team.dev)
        except Exception:
            log.exception("async supervisor: cannot resolve dev handle for %s", team_id)
            return
        await _run_async_agent(game, backend, _mgr_for(team_id), handle, team_id, cfg)

    game.register_async_supervisor_handler(supervisor)

    async def kill_all_agents() -> None:
        """Best-effort pkill of every team's claude process. Called by stop()
        before draining so an in-flight chunk returns promptly."""
        loop = asyncio.get_running_loop()
        for team in game.config.topology.teams:
            if not team.dev:
                continue
            try:
                handle = game.handle_for(team.dev)
            except Exception:
                continue
            mgr = _mgr_for(team.id)
            try:
                await loop.run_in_executor(
                    None,
                    lambda h=handle, m=mgr: backend.kill_process(h, m.PHASE_KILL_PATTERN),
                )
            except Exception:
                log.exception("kill_all_agents: pkill failed for %s", team.id)

    game.register_kill_all_agents(kill_all_agents)


if __name__ == "__main__":
    sys.exit(main())