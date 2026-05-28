"""DEFCON: defense kickoff on PHASE_CHANGE → DEFENSE.

Pins the round-loop integration: when the loop emits a DEFENSE
PHASE_CHANGE, GameMgr's subscriber publishes one DEFENSE_KICKOFF per
(team, defense-capable challenge) and invokes the registered handler.

Designed so the body doesn't need docker — we drive PHASE_CHANGE events
manually onto the bus and assert on the kickoff cascade. The actual
ClaudeAgentManager.install() is owned by an injected handler (here a
spy); GameMgr never spawns agents itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType, new_event
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _make_game(
    *,
    challenges: list[ChallengeConfig],
    teams: list[Team] | None = None,
    agents: dict[str, AgentConfig] | None = None,
) -> GameMgr:
    if teams is None:
        teams = [
            Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
            Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
        ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=challenges,
        rounds=RoundConfig(),
        agents=agents or {},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _phase_change(phase: Phase, round_id: int):
    return new_event(
        EventType.PHASE_CHANGE,
        round_id=round_id,
        phase=phase.value,
    )


# ── DEFENSE_KICKOFF emission ──────────────────────────────────────────


def test_defense_kickoff_emits_one_event_per_team_per_defense_capable_service():
    """A challenge whose spec has restart_cmd is defense-capable.
    For each defense-capable challenge × each team in its deploy_on,
    a DEFENSE_KICKOFF event must fire when DEFENSE phase enters."""
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1", "team2"],
            ),
        ],
    )

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))

    asyncio.run(run())

    kickoffs = [e for e in game.bus.history() if e.type == EventType.DEFENSE_KICKOFF.value]
    # One per (team, service). nautilus-ico × {team1, team2} → 2 events.
    assert len(kickoffs) == 2, [(e.team_id, e.payload) for e in kickoffs]
    team_ids = sorted(e.team_id for e in kickoffs)
    assert team_ids == ["team1", "team2"]
    for e in kickoffs:
        assert e.round_id == 1
        assert e.phase == Phase.DEFENSE.value
        assert e.payload["service"] == "nautilus-ico"


def test_defense_kickoff_skips_non_defense_capable_challenges():
    """notes has no restart_cmd in its spec metadata; it's not defense-capable
    even though deploy_on includes the teams. No DEFENSE_KICKOFF for it."""
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="notes", type="notes", port=5000, deploy_on=["team1", "team2"],
            ),
        ],
    )

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))

    asyncio.run(run())

    kickoffs = [e for e in game.bus.history() if e.type == EventType.DEFENSE_KICKOFF.value]
    assert kickoffs == [], (
        f"notes is not defense-capable; no kickoff expected. Got {[e.payload for e in kickoffs]}"
    )


def test_defense_kickoff_does_not_fire_outside_defense_phase():
    """ATTACK / SETUP / PREFLIGHT entry must not trigger defense kickoff."""
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1"],
            ),
        ],
    )

    async def run():
        # Every non-DEFENSE phase change.
        for p in (Phase.PREFLIGHT, Phase.SETUP, Phase.ATTACK):
            await game.bus.publish(_phase_change(p, round_id=1))

    asyncio.run(run())

    kickoffs = [e for e in game.bus.history() if e.type == EventType.DEFENSE_KICKOFF.value]
    assert kickoffs == []


def test_defense_kickoff_is_idempotent_within_a_single_round():
    """A re-fire of PHASE_CHANGE → DEFENSE for the same round must NOT
    spawn duplicates (defensive: a downstream might emit twice)."""
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1"],
            ),
        ],
    )

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))  # duplicate
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=2))  # new round, OK

    asyncio.run(run())

    kickoffs = [e for e in game.bus.history() if e.type == EventType.DEFENSE_KICKOFF.value]
    rounds = sorted(e.round_id for e in kickoffs)
    assert rounds == [1, 2], f"expected one per round, got {rounds}"


# ── Handler dispatch ──────────────────────────────────────────────────


def test_registered_defense_kickoff_handler_is_invoked_with_agent_config():
    """The injected handler must receive (team_id, AgentConfig) for each
    (team × defense-capable challenge). AgentConfig must carry the
    defense prompt (not the attack prompt)."""
    agents = {
        tid: AgentConfig(
            cli="claude --print",
            model="claude-sonnet-4-5",
            api_key="test-key",
            base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1", "team2"],
            ),
        ],
        agents=agents,
    )

    calls: list[tuple[str, AgentConfig]] = []

    async def handler(team_id: str, cfg: AgentConfig) -> None:
        calls.append((team_id, cfg))

    game.register_defense_kickoff_handler(handler)

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))

    asyncio.run(run())

    assert len(calls) == 2, calls
    by_team = {tid: cfg for tid, cfg in calls}
    assert set(by_team) == {"team1", "team2"}
    for tid, cfg in by_team.items():
        # Defense prompt fingerprints: mentions the service name + restart_service.
        assert "defense agent" in cfg.system_prompt.lower()
        assert "nautilus-ico" in cfg.system_prompt
        assert "restart_service" in cfg.system_prompt
        # Should NOT have attack-mode language. "PROTOCOL" alone is legitimate
        # in defense ("don't break the legitimate protocol"); the real attack
        # tells are "steal flags" + the LIST/GET notes commands or the ico
        # binary-protocol opcode crib.
        prompt = cfg.system_prompt
        assert "steal flags" not in prompt.lower()
        assert "Send: \"LIST" not in prompt
        assert "Send: \"GET" not in prompt
        # No opposing_prod_hosts in defense mode.
        assert cfg.opposing_prod_hosts == tuple()


def test_handler_exception_does_not_break_subsequent_handlers():
    """A handler raising for team1 must not stop the team2 kickoff —
    one team blowing up shouldn't halt the round."""
    agents = {
        tid: AgentConfig(
            cli="claude --print",
            model="claude-sonnet-4-5",
            api_key="test-key",
            base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1", "team2"],
            ),
        ],
        agents=agents,
    )

    invoked: list[str] = []

    async def handler(team_id: str, cfg: AgentConfig) -> None:
        invoked.append(team_id)
        if team_id == "team1":
            raise RuntimeError("simulated handler crash for team1")

    game.register_defense_kickoff_handler(handler)

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))

    asyncio.run(run())

    assert sorted(invoked) == ["team1", "team2"], (
        f"team2 must still be invoked after team1 raised; got {invoked}"
    )


def test_no_handler_registered_emits_events_silently():
    """The kickoff event must publish even when no agent handler is wired —
    observers (panel, tests) still want the signal."""
    game = _make_game(
        challenges=[
            ChallengeConfig(
                name="nautilus-ico", type="nautilus-ico",
                port=4265, deploy_on=["team1"],
            ),
        ],
    )
    # No handler registered.

    async def run():
        await game.bus.publish(_phase_change(Phase.DEFENSE, round_id=1))

    asyncio.run(run())

    kickoffs = [e for e in game.bus.history() if e.type == EventType.DEFENSE_KICKOFF.value]
    assert len(kickoffs) == 1
