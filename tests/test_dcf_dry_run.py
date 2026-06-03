"""Pre-R1 dry-run readiness probe.

GameMgr.dry_run_all_agents runs one short claude invocation per team
asking it to call `mcp__cyberarena__report_ready`. Success is observed
on the bus via AGENT_READY → DRY_RUN_OK; timeout / handler crash →
DRY_RUN_FAILED.

These tests stub the handler with in-memory shapes (no docker, no
real claude); they verify the **coordination** logic, not the agent
invocation itself (that's covered by test_dcf_phase_invocation.py).
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _make_game() -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1"])],
        rounds=RoundConfig(),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _event_types_for_team(game: GameMgr, team_id: str) -> list[str]:
    return [e.type for e in game.timeline.history() if e.team_id == team_id]


# ── no handler registered ────────────────────────────────────────────


def test_dry_run_all_agents_noop_when_no_handler():
    """Without `register_dry_run_handler`, the method returns immediately
    and emits nothing — keeps test-mode `start()` runs cheap."""
    game = _make_game()
    asyncio.run(game.dry_run_all_agents())
    assert game.timeline.history() == []


# ── happy path: AGENT_READY arrives → DRY_RUN_OK ─────────────────────


def test_dry_run_emits_ok_when_handler_triggers_agent_ready():
    """Handler simulates the real agent by calling
    `game.record_agent_ready(team_id)` — that fires AGENT_READY on the
    bus, which the dry-run coordinator observes per team."""
    game = _make_game()

    async def handler(team_id: str) -> None:
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    for team_id in ("team1", "team2"):
        types = _event_types_for_team(game, team_id)
        assert types[0] == EventType.DRY_RUN_START.value, types
        assert EventType.AGENT_READY.value in types
        assert types[-1] == EventType.DRY_RUN_OK.value, types
        assert EventType.DRY_RUN_FAILED.value not in types


# ── handler runs but never triggers AGENT_READY → DRY_RUN_FAILED ─────


def test_dry_run_emits_failed_when_no_agent_ready():
    """Handler returns cleanly but never causes AGENT_READY (e.g. agent
    crashed silently). Dry-run coordinator catches the missing signal
    and emits DRY_RUN_FAILED with a reason."""
    game = _make_game()

    invoked: list[str] = []
    async def handler(team_id: str) -> None:
        invoked.append(team_id)
        # No call to record_agent_ready — simulates broken setup.

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    assert sorted(invoked) == ["team1", "team2"]
    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        assert "AGENT_READY" in failed[0].payload.get("error", "")


# ── handler raises → DRY_RUN_FAILED with exception text ──────────────


def test_dry_run_emits_failed_when_handler_raises():
    game = _make_game()

    async def handler(team_id: str) -> None:
        raise RuntimeError(f"simulated crash for {team_id}")

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        err = failed[0].payload.get("error", "")
        assert "RuntimeError" in err
        assert team_id in err


# ── handler hangs past timeout → DRY_RUN_FAILED ──────────────────────


def test_dry_run_emits_failed_when_handler_hangs():
    """If the agent never returns from the per-team handler within
    timeout_secs, dry-run coordinator gives up and emits FAILED. The
    underlying claude process should be killed by the runner's
    transport-level timeout (not exercised here — that's the runner's
    test surface)."""
    game = _make_game()

    async def handler(team_id: str) -> None:
        await asyncio.sleep(60)  # way past our timeout

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.05))

    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        assert "exceeded" in failed[0].payload.get("error", "").lower()


# ── isolation: one team failing doesn't break the other ─────────────


def test_dry_run_one_team_failing_does_not_block_other():
    game = _make_game()

    async def handler(team_id: str) -> None:
        if team_id == "team1":
            raise RuntimeError("simulated team1 crash")
        # team2: succeed normally.
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    # team1 → FAILED
    assert any(
        e.type == EventType.DRY_RUN_FAILED.value and e.team_id == "team1"
        for e in game.timeline.history()
    )
    # team2 → OK
    assert any(
        e.type == EventType.DRY_RUN_OK.value and e.team_id == "team2"
        for e in game.timeline.history()
    )
