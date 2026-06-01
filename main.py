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
from agents.runner import run_phase_via_transport
from agents.transport_docker import DockerExecBackend
from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from models.agent import AgentConfig
from orchestrator import topology
from orchestrator.orchestrator import Orchestrator

log = logging.getLogger("main")

DEFAULT_CONFIG = Path(__file__).parent / "config" / "game.yaml"


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
    cfg = load_game_config(args.config)
    # Build challenge images before Orchestrator.setup() — same reason
    # as the `setup` subcommand path.
    _build_challenge_images(args.config)
    orch = Orchestrator(graph=cfg.topology)
    orch.setup()

    game = GameMgr(config=cfg, orchestrator=orch)
    game.deploy_challenges()
    game.plant_initial_flags()

    # Persistent-memory v1: install once pre-loop, run_phase per phase.
    # Wires install + defense/attack kickoff handlers on game; the
    # install handler runs inside game.start() (after FileSink is up)
    # via game.install_all_agents().
    _wire_phase_kickoffs(game)

    shutdown = asyncio.Event()
    app = build_app(game, shutdown)

    uv_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(uv_config)

    loop_task = asyncio.create_task(game.start(), name="round-loop")
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

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

    # Tell uvicorn to wind down. `should_exit` triggers graceful shutdown
    # (drains in-flight requests + closes idle keep-alives), but it can
    # hang indefinitely on long-lived streams — panel WebSockets and the
    # MCP StreamableHTTP SSE sessions are both "long-lived" by design.
    # Escalate to `force_exit` (drops connections) after a short grace,
    # then to outright task cancellation if even that doesn't fire.
    server.should_exit = True
    try:
        await asyncio.wait_for(server_task, timeout=10)
    except asyncio.TimeoutError:
        log.warning("uvicorn didn't exit within 10s; setting force_exit")
        server.force_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except asyncio.TimeoutError:
            log.error("uvicorn force_exit also stalled; cancelling task")
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
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

    Delegates to `challenges.registry.build_all_images` — the registry
    owns the source set, so adding a new adapter (picoCTF, custom git)
    only needs one branch there, not here.

    Idempotent: each source's `build_image` skips when the tag is already
    present locally.
    """
    from challenges.registry import build_all_images

    cfg = load_game_config(config_path)
    tags = build_all_images(cfg)
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
        run_phase_via_transport → record_phase_done. All CLI-specific
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
                result = await run_phase_via_transport(
                    mgr, backend, handle, prompt,
                    timeout=timeout_secs, model=cfg.model or None,
                )
            except Exception as e:
                log.exception("%s kickoff: run_phase raised for %s", phase, team_id)
                from gamemgr.events import EventType, new_event
                await game.bus.publish(new_event(
                    EventType.AGENT_FAILED,
                    team_id=team_id, round_id=round_id, phase=phase,
                    payload={"error": f"{type(e).__name__}: {e}"[:200]},
                ))
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