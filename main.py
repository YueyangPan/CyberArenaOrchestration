"""Cyber Arena orchestrator CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import docker
import uvicorn

from agents.claude import ClaudeAgentManager
from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
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

    # DEFCON Phase 4b: spawn a defense agent on each team's dev container
    # when the round loop enters DEFENSE. Handler is sync (ClaudeAgentManager.install
    # is sync) → run in the default thread pool to avoid blocking the loop.
    _wire_defense_kickoff(game)

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
    server.should_exit = True
    await server_task
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


def _wire_defense_kickoff(game: GameMgr) -> None:
    """Register defense + attack kickoff handlers that install a Claude
    agent on the team's dev container on every DEFENSE / ATTACK entry.

    Both phases share one ClaudeAgentManager (state is per-container, not
    per-handler) so subsequent installs on the same dev container reuse
    SSH keys / npm-installed CLI / etc.

    Sync `install()` is bridged into the async loop via run_in_executor.
    The kickoff event also publishes regardless — observers (panel) see
    the signal even if the install raises.
    """
    client = docker.from_env()
    agent_mgr = ClaudeAgentManager(client=client)

    def _make_handler(mode: str):
        async def handler(team_id: str, cfg) -> None:
            team = next((t for t in game.config.topology.teams if t.id == team_id), None)
            if team is None or not team.dev:
                log.warning("%s kickoff: team %s has no dev container; skipping", mode, team_id)
                return
            try:
                handle = game._handle_for(team.dev)
            except Exception:
                log.exception("%s kickoff: cannot resolve dev handle for %s", mode, team_id)
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, agent_mgr.install, handle, cfg)
            log.info("%s agent installed on %s", mode, team.dev)
        return handler

    game.register_defense_kickoff_handler(_make_handler("defense"))
    game.register_attack_kickoff_handler(_make_handler("attack"))
    # Artifact persistence — same agent_mgr reads stdout/stderr/exit_code
    # off the dev container so the run_dir gets api-logs/ + agent-logs/.
    # docker logs → service-logs/ are owned by GameMgr directly (no
    # AgentManager needed for that path).
    game.register_fetch_agent_logs_handler(agent_mgr.fetch_logs)


if __name__ == "__main__":
    sys.exit(main())