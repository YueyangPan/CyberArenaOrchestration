"""DEFCON: defense kickoff at DEFENSE phase entry.

Pins the per-team-per-defense-capable-service kickoff cascade. The
"doesn't fire outside DEFENSE" guard is now structurally ensured
(only enter_defense_phase records DEFENSE_KICKOFF), so its explicit
test was dropped.
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
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


def _defense_kickoffs(game: GameMgr) -> list:
    return [e for e in game.timeline.history() if e.type == EventType.DEFENSE_KICKOFF.value]


# ── DEFENSE_KICKOFF emission ──────────────────────────────────────────


def test_defense_kickoff_records_one_event_per_team_per_defense_capable_service():
    """nautilus-ico has restart_cmd in spec → defense-capable. One event
    per (team, service)."""
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1", "team2"],
        )],
    )
    game.enter_defense_phase(round_id=1)

    kickoffs = _defense_kickoffs(game)
    assert len(kickoffs) == 2
    assert sorted(e.team_id for e in kickoffs) == ["team1", "team2"]
    for e in kickoffs:
        assert e.round_id == 1
        assert e.phase == Phase.DEFENSE.value
        assert e.payload["service"] == "nautilus-ico"


def test_defense_kickoff_skips_non_defense_capable_challenges():
    """notes has no restart_cmd; not defense-capable."""
    game = _make_game(
        challenges=[ChallengeConfig(
            name="notes", type="notes", port=5000, deploy_on=["team1", "team2"],
        )],
    )
    game.enter_defense_phase(round_id=1)
    assert _defense_kickoffs(game) == []


def test_flag_planted_fires_before_defense_kickoff():
    """REGRESSION: enter_defense_phase must plant + record FLAG_PLANTED
    BEFORE spawning kickoff handlers. Used to be a bus-ordering bug; now
    enforced by the method body (rotate → record → spawn).
    """
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1", "team2"],
        )],
        agents={
            tid: AgentConfig(
                cli="claude --print", model="claude-sonnet-4-5",
                api_key="test", base_url="https://api.anthropic.com",
            )
            for tid in ("team1", "team2")
        },
    )
    # Inject a stub Challenge so plant_flags has work to do but doesn't
    # touch docker. The stub's flag_handler.plant is a no-op; FlagServer
    # still records FLAG_PLANTED and updates the store, which is what
    # this regression test pins.
    from challenges.interface import (
        Challenge, ChallengeDocs, FlagHandler, FunctionalityTest, VulboxImage,
    )

    class _StubFH(FlagHandler):
        @property
        def name(self): return "stub-fh"
        def plant(self, target, flag): return "/flag"
        def retrieve(self, target, handle): return None

    class _StubFT(FunctionalityTest):
        @property
        def name(self): return "stub-ft"
        def run(self, target): raise NotImplementedError

    class _StubCh(Challenge):
        @property
        def name(self): return "nautilus-ico"
        @property
        def vulbox(self):
            return VulboxImage(reference="x", exposed_ports={"service": 4265})
        @property
        def functionality_test(self): return _StubFT()
        @property
        def flag_handler(self): return _StubFH()
        @property
        def docs(self):
            return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")

    stub = _StubCh()
    game._challenge_by_owner[("team1", "nautilus-ico")] = stub
    game._challenge_by_owner[("team2", "nautilus-ico")] = stub

    seen_history_at_handler: dict[str, list[str]] = {}
    seen_planted_at_handler: dict[str, dict] = {}

    async def handler(team_id, cfg, round_id, phase, is_first):
        seen_history_at_handler[team_id] = [e.type for e in game.timeline.history()]
        seen_planted_at_handler[team_id] = dict(game.flags.planted)

    game.register_defense_kickoff_handler(handler)

    async def run():
        tasks = game.enter_defense_phase(round_id=1)
        await asyncio.gather(*tasks)

    asyncio.run(run())

    for team_id in ("team1", "team2"):
        assert "FLAG_PLANTED" in seen_history_at_handler[team_id], (
            f"{team_id} handler ran before FLAG_PLANTED — rotation order regression"
        )
        assert (team_id, "nautilus-ico") in seen_planted_at_handler[team_id]


def test_defense_kickoff_is_idempotent_within_a_round():
    """Re-entering DEFENSE in the same round MUST NOT double-spawn."""
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1"],
        )],
    )
    game.enter_defense_phase(round_id=1)
    game.enter_defense_phase(round_id=1)  # duplicate
    game.enter_defense_phase(round_id=2)
    kickoffs = _defense_kickoffs(game)
    assert sorted(e.round_id for e in kickoffs) == [1, 2]


# ── Handler dispatch ──────────────────────────────────────────────────


def test_registered_defense_kickoff_handler_is_invoked_with_agent_config():
    """Handler receives a defense-mode AgentConfig per (team × service)."""
    agents = {
        tid: AgentConfig(
            cli="claude --print", model="claude-sonnet-4-5",
            api_key="test-key", base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1", "team2"],
        )],
        agents=agents,
    )
    calls: list[tuple[str, AgentConfig, int, str, bool]] = []

    async def handler(team_id, cfg, round_id, phase, is_first):
        calls.append((team_id, cfg, round_id, phase, is_first))

    game.register_defense_kickoff_handler(handler)

    async def run():
        tasks = game.enter_defense_phase(round_id=1)
        await asyncio.gather(*tasks)

    asyncio.run(run())

    assert len(calls) == 2
    by_team = {c[0]: c[1] for c in calls}
    assert set(by_team) == {"team1", "team2"}
    assert all(c[4] is True for c in calls)
    assert all(c[2] == 1 and c[3] == "defense" for c in calls), calls
    for tid, cfg in by_team.items():
        assert "defense agent" in cfg.system_prompt.lower()
        assert "nautilus-ico" in cfg.system_prompt
        assert "restart_service" in cfg.system_prompt
        assert "steal flags" not in cfg.system_prompt.lower()
        assert cfg.opposing_prod_hosts == tuple()


def test_handler_exception_does_not_break_subsequent_handlers():
    """team1's handler crash must not stop team2's handler."""
    agents = {
        tid: AgentConfig(
            cli="claude --print", model="claude-sonnet-4-5",
            api_key="test-key", base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1", "team2"],
        )],
        agents=agents,
    )
    invoked: list[str] = []

    async def handler(team_id, cfg, round_id, phase, is_first):
        invoked.append(team_id)
        if team_id == "team1":
            raise RuntimeError("simulated handler crash for team1")

    game.register_defense_kickoff_handler(handler)

    async def run():
        tasks = game.enter_defense_phase(round_id=1)
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    assert sorted(invoked) == ["team1", "team2"]


def test_no_handler_registered_records_events_silently():
    """Observers still want DEFENSE_KICKOFF in the timeline even without an agent."""
    game = _make_game(
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1"],
        )],
    )
    game.enter_defense_phase(round_id=1)
    assert len(_defense_kickoffs(game)) == 1
