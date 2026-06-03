"""DEFCON: restart_service machinery + MCP tool wire-up.

Mocks docker exec (we don't have docker in this sandbox) so the
async job lifecycle, event ordering, and in-flight rejection can be
verified independently. Real docker integration is exercised by the
ico live tests once those land.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


# ── Helpers ────────────────────────────────────────────────────────────


class _StubOrchestrator:
    """GameMgr only uses orchestrator.network during the round loop; tests
    that don't drive the loop can get away with a bare object."""

    network = None


def _make_game(challenge_name: str = "nautilus-ico") -> GameMgr:
    """Build a minimal GameMgr with one team + one challenge, no docker."""
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[
            ChallengeConfig(name=challenge_name, type=challenge_name, port=4265, deploy_on=["team1", "team2"]),
        ],
        rounds=RoundConfig(),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _events_of(bus, type_: str) -> list:
    return [e for e in bus.history() if e.type == type_]


async def _wait_for(bus, type_: str, timeout: float = 5.0):
    """Spin until at least one event of `type_` lands on the bus."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _events_of(bus, type_):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {type_}; got {[e.type for e in bus.history()]}")


# ── Tests ──────────────────────────────────────────────────────────────


def test_restart_service_unknown_service_raises():
    """An unknown service name raises ValueError (MCP layer maps to InvalidParams)."""
    game = _make_game()

    async def run():
        with pytest.raises(ValueError, match="unknown service"):
            await game.restart_service("team1", "not-a-thing")

    asyncio.run(run())


def test_restart_service_notes_has_no_restart_cmd():
    """The toy `notes` source has no restart_cmd in its spec metadata —
    restart_service rejects it as not-defendable rather than crashing later."""
    game = _make_game(challenge_name="notes")

    async def run():
        with pytest.raises(ValueError, match="no restart_cmd"):
            await game.restart_service("team1", "notes")

    asyncio.run(run())


def test_restart_service_unknown_team():
    """A team not in topology raises ValueError."""
    game = _make_game()

    async def run():
        with pytest.raises(ValueError, match="unknown team"):
            await game.restart_service("team-bogus", "nautilus-ico")

    asyncio.run(run())


def test_restart_service_happy_path_emits_started_completed_with_health_chain(monkeypatch):
    """Mock docker exec to a no-op success; assert the full event chain."""
    game = _make_game()
    bus = game.timeline

    # Mock _exec_in_container so docker isn't required. Return (0, "ok") for
    # restart + all health checks.
    def fake_exec(self, container_name, cmd):
        return (0, "ok\n")

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            # restart_service routes its post-restart probe through the
            # HealthPoller — build the facility before driving the test
            # (production code does this inside start()).
            game._build_health_facility()
            job_id = await game.restart_service("team1", "nautilus-ico")
            assert isinstance(job_id, str) and len(job_id) == 12
            # Background task is running; let it finish.
            await _wait_for(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            # Give the health chain a tick to land too.
            for _ in range(50):
                if len(_events_of(bus, EventType.SERVICE_HEALTH_CHECK.value)) >= 4:
                    break
                await asyncio.sleep(0.02)

            # Restart pair: STARTED ts ≤ COMPLETED ts; matching job_id.
            started = _events_of(bus, EventType.SERVICE_RESTART_STARTED.value)
            completed = _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            assert len(started) == 1 and len(completed) == 1
            assert started[0].payload["job_id"] == completed[0].payload["job_id"] == job_id
            assert completed[0].payload["duration_ms"] >= 0

            # No FAILED on happy path.
            assert _events_of(bus, EventType.SERVICE_RESTART_FAILED.value) == []

            # Health chain: exactly 4 events, all 4 HealthLevels, ordered.
            checks = _events_of(bus, EventType.SERVICE_HEALTH_CHECK.value)
            assert len(checks) == 4, f"expected 4 health checks, got {[c.payload for c in checks]}"
            levels = [c.payload["level"] for c in checks]
            assert levels == ["process", "tcp", "protocol", "checker"]
            # All passed=True with our no-op-success mock.
            for c in checks:
                assert c.payload["passed"] is True, f"check failed: {c.payload}"

        asyncio.run(run())


def test_restart_service_failed_path_emits_failed_no_health(monkeypatch):
    """A non-zero exit from restart_cmd → FAILED event, no health chain runs."""
    game = _make_game()
    bus = game.timeline

    def fake_exec(self, container_name, cmd):
        return (1, "fpc: syntax error in ico.pas\n")

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            job_id = await game.restart_service("team1", "nautilus-ico")
            await _wait_for(bus, EventType.SERVICE_RESTART_FAILED.value)

            failed = _events_of(bus, EventType.SERVICE_RESTART_FAILED.value)
            assert len(failed) == 1
            assert failed[0].payload["job_id"] == job_id
            assert "syntax error" in failed[0].payload["error"]

            # No COMPLETED. No health checks (we didn't get past the restart).
            assert _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value) == []
            assert _events_of(bus, EventType.SERVICE_HEALTH_CHECK.value) == []

        asyncio.run(run())


def test_restart_service_in_flight_rejected():
    """A second restart for the same (team, service) while the first is
    still running rejects with ValueError → InvalidParams at MCP layer."""
    game = _make_game()
    started_event = threading.Event()
    block_event = threading.Event()

    def slow_exec(self, container_name, cmd):
        # Signal that we've started, then block until release.
        started_event.set()
        block_event.wait(timeout=5)
        return (0, "ok\n")

    with patch.object(GameMgr, "_exec_in_container", slow_exec):
        async def run():
            job1 = await game.restart_service("team1", "nautilus-ico")
            # Wait until the background task has actually entered _exec_in_container,
            # i.e. the in-flight record exists and is not yet "done".
            await asyncio.get_running_loop().run_in_executor(None, started_event.wait, 5)
            # Second restart for same (team, service) should reject.
            with pytest.raises(ValueError, match="already in flight"):
                await game.restart_service("team1", "nautilus-ico")
            # A different team is fine.
            job_other_team = await game.restart_service("team2", "nautilus-ico")
            assert job_other_team != job1
            block_event.set()  # let the workers finish

        asyncio.run(run())
