"""Cyber Arena orchestrator CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator import topology
from orchestrator.orchestrator import Orchestrator

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
    orch = Orchestrator(graph=cfg.topology)
    orch.setup()

    game = GameMgr(config=cfg, orchestrator=orch)
    game.deploy_challenges()

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


if __name__ == "__main__":
    sys.exit(main())