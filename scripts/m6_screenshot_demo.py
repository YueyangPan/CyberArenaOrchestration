#!/usr/bin/env python3
"""Live M5.6-style attack + M6 panel screenshots.

Runs end-to-end:

  1. Bring up arena (M1) + deploy challenge (M2) + plant initial flags (M5).
  2. Start uvicorn on :8000 so the panel + submit endpoint are live.
  3. Snap screenshot #1 — panel after plant, before agent.
  4. Install Claude attack agent on team2_dev (M5.6 pipeline: SSH pivot
     provisioned inside install()).
  5. Poll FlagStore until team2 lands at least one ACCEPTED submission, or
     time out (default 4 min).
  6. Snap screenshot #2 — panel after attack.
  7. Tear down.

Requires API_ROUTER_KEY in .env (real Claude is invoked; costs ~10-30¢).
Skips with a clear message if not set.

Output: docs/screenshots/m5_6_attack/{01_after_plant,02_after_attack}.png
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import docker
import uvicorn
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agents.claude import ClaudeAgentManager  # noqa: E402
from gamemgr.api import build_app  # noqa: E402
from gamemgr.config import load as load_game_config  # noqa: E402
from gamemgr.events import EventType, new_event  # noqa: E402
from gamemgr.game_mgr import GameMgr  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.service import ContainerHandle  # noqa: E402

CONFIG = REPO / "config" / "game.yaml"
SHOTS_DIR = REPO / "docs" / "screenshots" / "m5_6_attack"
PORT = 8000
WAIT_S = 240
POLL_S = 5


def _have_key() -> bool:
    load_dotenv(REPO / ".env", override=False)
    k = os.environ.get("API_ROUTER_KEY", "")
    return bool(k) and "placeholder" not in k


def _chrome_path() -> str | None:
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _shot(out: Path, label: str, chrome: str, port: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"  📸 screenshot {label} -> {out.relative_to(REPO)}")
    # virtual-time-budget=8000 gives the panel JS plenty of time to
    # fetch /game/events, parse, and render — the prior 3000 was cutting
    # off the WS handshake before any events drew.
    r = subprocess.run(
        [
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars",
            "--window-size=1400,900",
            f"--screenshot={out}",
            "--virtual-time-budget=8000",
            "--run-all-compositor-stages-before-draw",
            f"http://127.0.0.1:{port}/panel",
        ],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"  ⚠️  chrome failed: {r.stderr.decode(errors='replace')[:400]}", file=sys.stderr)


def _handle(client, name: str) -> ContainerHandle:
    c = client.containers.get(name)
    return ContainerHandle(
        container_id=c.id, name=name,
        networks=list(c.attrs["NetworkSettings"]["Networks"].keys()),
    )


def main() -> int:
    if not _have_key():
        print("set API_ROUTER_KEY in .env first (real Claude is invoked, ~10-30¢/run)", file=sys.stderr)
        return 2
    chrome = _chrome_path()
    if chrome is None:
        print("no google-chrome / chromium on PATH; install one to capture screenshots", file=sys.stderr)
        return 2

    client = docker.from_env(); client.ping()

    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)

    orch.setup()
    try:
        game.deploy_challenges()
        for service, handles in game._deployed:
            for h in handles:
                for _ in range(50):
                    if service.is_alive(h): break
                    time.sleep(0.1)
        game.plant_initial_flags()

        th.start()
        for _ in range(30):
            if server.started: break
            time.sleep(0.1)

        # Emit SERVICE_DEPLOYED + FLAG_PLANTED so the panel has content even
        # though we never start the round loop.
        asyncio.run(game.emit_setup_events())

        time.sleep(0.5)
        _shot(SHOTS_DIR / "01_after_plant.png", "after plant", chrome, PORT)

        print("  🤖 installing attack agent on team2_dev …")
        agent_cfg = game.agent_config_for("team2")
        mgr = ClaudeAgentManager(client=client)
        mgr.install(_handle(client, "team2_dev"), agent_cfg)

        # AGENT_INSTALLED is in the EventType enum (M4 design) but no
        # production code path emits it yet. Surface it here so the panel
        # can show the installed-agent chip. Future M4.5 will emit from
        # ClaudeAgentManager directly.
        asyncio.run(game.bus.publish(new_event(
            EventType.AGENT_INSTALLED,
            team_id="team2",
            payload={"cli": agent_cfg.cli.split()[0]},
        )))

        deadline = time.monotonic() + WAIT_S
        last_log = 0.0
        while time.monotonic() < deadline:
            accepted = [e for e in game.bus.history()
                        if e.type == "FLAG_SUBMITTED" and e.payload.get("accepted")]
            if accepted:
                print(f"  ✅ ACCEPTED submission landed after {int(WAIT_S - (deadline - time.monotonic()))}s")
                break
            if time.monotonic() - last_log > 30:
                print(f"  ...waiting (~{int(WAIT_S - (deadline - time.monotonic()))}s elapsed)")
                last_log = time.monotonic()
            time.sleep(POLL_S)
        else:
            print("  ⚠️  no ACCEPTED in time — capturing anyway", file=sys.stderr)

        time.sleep(1.5)
        _shot(SHOTS_DIR / "02_after_attack.png", "after attack", chrome, PORT)

        try: mgr.kill(_handle(client, "team2_dev"))
        except Exception: pass

    finally:
        try:
            server.should_exit = True
            th.join(timeout=5)
        except Exception: pass
        try: game.teardown_challenges()
        except Exception: pass
        orch.teardown()

    print(f"\ndone. screenshots in {SHOTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
