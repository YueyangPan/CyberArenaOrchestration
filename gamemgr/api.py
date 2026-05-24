"""FastAPI surface for GameMgr — REST endpoints plus a WebSocket event stream."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .events import Event
from .game_mgr import GameMgr

log = logging.getLogger(__name__)


def build_app(game: GameMgr, shutdown_event: asyncio.Event) -> FastAPI:
    app = FastAPI(title="cyber-arena")

    @app.get("/game/status")
    async def status():
        return game.status()

    @app.get("/game/rounds")
    async def rounds():
        st = game.status()
        return {"current_round": st["round"], "total_rounds": st["total_rounds"]}

    @app.get("/game/events")
    async def events(since: float | None = None) -> list[dict[str, Any]]:
        hist = game.bus.history()
        if since is not None:
            hist = [e for e in hist if e.ts >= since]
        return [asdict(e) for e in hist]

    @app.post("/game/start")
    async def start():
        return {"ok": True, "note": "game loop already running (started at boot)"}

    @app.post("/game/stop")
    async def stop():
        await game.stop()
        shutdown_event.set()
        return {"ok": True}

    @app.post("/game/flag/submit")
    async def submit_flag(body: dict):
        result = await game.submit_flag(body.get("team_id", ""), body.get("flag", ""))
        return {"accepted": result.accepted, "reason": result.reason}

    @app.websocket("/events/stream")
    async def events_stream(websocket: WebSocket):
        await websocket.accept()
        queue: asyncio.Queue[Event] = asyncio.Queue()

        async def push(event: Event) -> None:
            await queue.put(event)

        game.bus.subscribe(push)
        try:
            for past in game.bus.history():
                await websocket.send_json(asdict(past))
            while True:
                event = await queue.get()
                await websocket.send_json(asdict(event))
        except WebSocketDisconnect:
            pass
        finally:
            game.bus.unsubscribe(push)

    return app