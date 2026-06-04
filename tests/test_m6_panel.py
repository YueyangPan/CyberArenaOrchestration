"""M6 — panel smoke.

Three layers:
  1. file existence + required HTML hooks (no FastAPI, no browser)
  2. FastAPI TestClient: GET /panel returns the file, /game/events returns
     JSON the panel JS can consume
  3. Optional headless-chrome render: skipped if google-chrome isn't on PATH;
     otherwise renders /panel and asserts a non-trivial PNG comes out
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamemgr.api import PANEL_HTML_PATH, build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "game.yaml"


# ── layer 1: file + required IDs ────────────────────────────


def test_panel_html_exists():
    assert PANEL_HTML_PATH.is_file(), f"panel missing: {PANEL_HTML_PATH}"
    content = PANEL_HTML_PATH.read_text()
    # Anchors the panel JS / report-screenshot pipeline depends on:
    for needle in (
        'id="round-num"', 'id="phase"',
        'id="timeline"', 'id="controls"', 'id="conn"',
        'fetch(\'/game/events\')',     # backfill path
        '/events/stream',              # WS path
        'Cyber Arena',                 # title text
    ):
        assert needle in content, f"panel.html missing hook: {needle!r}"


# ── layer 2: FastAPI surface ────────────────────────────────


@pytest.fixture
def app_client(monkeypatch):
    # GameMgr requires a topology config; just load the real game.yaml — no
    # docker actions taken until you call orch.setup(), which we don't.
    cfg = load_game_config(CONFIG)
    # We never start docker here; mocking an Orchestrator with the minimum
    # surface GameMgr needs at construction:
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    shutdown = asyncio.Event()
    return TestClient(build_app(game, shutdown))


def test_panel_route_serves_html(app_client):
    r = app_client.get("/panel")
    assert r.status_code == 200
    assert "Cyber Arena" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_events_route_is_json_array(app_client):
    r = app_client.get("/game/events")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)   # empty pre-start, but valid JSON


# ── layer 3: real chrome render (skipped if chrome absent) ──


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.mark.skipif(CHROME is None, reason="no chrome on PATH")
def test_panel_renders_in_headless_chrome():
    """Bring up a real uvicorn, point chrome at /panel, snap a screenshot,
    sanity-check that something non-trivial was rendered."""
    import uvicorn

    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    port = 8765   # dedicated test port
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    # wait for bind
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn never bound"

    try:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "panel.png")
            rc = subprocess.run(
                [
                    CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars",
                    "--window-size=1400,900",
                    f"--screenshot={out}",
                    f"--virtual-time-budget=1500",  # let JS run for 1.5s
                    f"http://127.0.0.1:{port}/panel",
                ],
                capture_output=True, timeout=30,
            )
            assert rc.returncode == 0, f"chrome failed: {rc.stderr.decode(errors='replace')!r}"
            size = os.path.getsize(out)
            assert size > 5000, f"screenshot suspiciously small ({size} bytes)"
    finally:
        server.should_exit = True
        th.join(timeout=5)
