"""FastAPI surface for GameMgr — REST endpoints plus a WebSocket event stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .events import Event
from .game_mgr import GameMgr
from .mcp_server import MCP_MOUNT_PATH, MCP_PUBLIC_ENDPOINT, build_mcp_app
from .transcripts import collect_transcripts

PANEL_HTML_PATH = Path(__file__).parent.parent / "panel" / "panel.html"

log = logging.getLogger(__name__)


def build_app(game: GameMgr, shutdown_event: asyncio.Event) -> FastAPI:
    # MCP server must be built before the FastAPI lifespan closure because
    # the lifespan needs a reference to the FastMCP instance to run its
    # session manager (otherwise tools error out with "Task group is not
    # initialized" on the first request).
    mcp_server, mcp_asgi = build_mcp_app(game)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info(
            "cyberarena MCP endpoint: http://<host>:<port>%s",
            MCP_PUBLIC_ENDPOINT,
        )
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="cyber-arena", lifespan=lifespan)
    app.mount(MCP_MOUNT_PATH, mcp_asgi)

    @app.get("/panel")
    async def panel_page():
        """Serve panel/panel.html. Same-origin with /game/* + /events/stream
        so the browser can WS without CORS / mixed-content fuss."""
        return FileResponse(PANEL_HTML_PATH, media_type="text/html")

    @app.get("/game/status")
    async def status():
        return game.status()

    @app.get("/game/rounds")
    async def rounds():
        st = game.status()
        return {"current_round": st["round"], "total_rounds": st["total_rounds"]}

    @app.get("/game/agents")
    async def agents() -> dict[str, Any]:
        """Parsed agent stream-json transcripts for the current run,
        grouped by team → round → phase. Read live from the run's
        artifact dir; empty until the first phase completes (transcripts
        are written at phase end by record_phase_done). Carries full
        agent output — tool commands, reasoning, flag strings — so it
        shares the panel's open posture and is not flag-redacted."""
        if game.run_dir is None:
            return {"run": None, "transcripts": []}
        return {
            "run": game.run_dir.name,
            "transcripts": collect_transcripts(game.run_dir),
        }

    @app.get("/game/events")
    async def events(since: float | None = None) -> list[dict[str, Any]]:
        hist = game.timeline.history()
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
    async def submit_flag(body: dict, x_team_token: str | None = Header(default=None)):
        # Header is the only authoritative identity. body['team_id'] (if
        # present, from old M5 clients) is ignored. BAD_TOKEN does not
        # emit a FLAG_SUBMITTED event — we don't want the event stream to
        # become a token-probing oracle.
        if not x_team_token:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        team_id = game._token_to_team.get(x_team_token)
        if team_id is None:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        result = await game.submit_flag(team_id, body.get("flag", ""))
        return {
            "accepted": result.accepted,
            "reason": result.reason,
            "detail": result.detail,
        }

    @app.websocket("/events/stream")
    async def events_stream(websocket: WebSocket):
        await websocket.accept()
        # attach_stream gives this connection its own queue; record() fans
        # out via put_nowait so a slow client cannot back-pressure the game.
        queue = game.timeline.attach_stream()
        try:
            for past in game.timeline.history():
                await websocket.send_json(asdict(past))
            while True:
                event = await queue.get()
                await websocket.send_json(asdict(event))
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Client closed, or the server is shutting down and cancelled
            # this long-lived stream — both are clean exits, not errors.
            # (Swallowing CancelledError here keeps uvicorn's force-exit
            # from logging "Exception in ASGI application" for the panel WS.)
            pass
        finally:
            game.timeline.detach_stream(queue)

    return app