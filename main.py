"""Cyber Arena orchestrator CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import time

import docker
import uvicorn

from agents.manager import AgentManager
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

    # Persistent-memory v1: install once pre-loop, run_phase per phase.
    # Wires install + defense/attack kickoff handlers on game.
    _wire_phase_kickoffs(game)

    # Open the run + bring the API server up BEFORE the arena bring-up so
    # the live panel can connect and stream the bring-up events.
    game.begin_run()
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

    # Tell uvicorn to wind down. `should_exit` triggers graceful shutdown
    # (drains in-flight requests + closes idle keep-alives), but it can
    # hang indefinitely on long-lived streams — panel WebSockets and the
    # MCP StreamableHTTP SSE sessions are both "long-lived" by design.
    # Escalate to `force_exit` (drops connections) after a short grace,
    # then to outright task cancellation if even that doesn't fire.
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


def _wire_phase_kickoffs(game: GameMgr) -> None:
    """Wire the agent layer into GameMgr for persistent-memory v1.

    One transport (DockerExecBackend), one manager per provider
    (resolved via `agents.registry.build`). Three handlers:

      * `install_handler`  — runs once per team from
        `game.install_all_agents()` (pre-loop). Bridges the sync
        `AgentManager.install` into the loop via run_in_executor.
      * `_make_phase_handler` (defense + attack) — runs per (team,
        phase) from the kickoff subscribers. The whole body is the
        5-line dance: lookup handle → record_phase_started →
        mgr.run_phase → record_phase_done. All CLI-specific
        details (prompt path, claude argv, kill pattern) live behind
        `mgr.PROMPT_PATH` / `mgr.phase_command()` / `mgr.PHASE_KILL_PATTERN`.
    """
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


if __name__ == "__main__":
    sys.exit(main())