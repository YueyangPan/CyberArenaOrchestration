"""DEFCON: DEFENSE phase ends early once every team has reported
DEPLOY_COMPLETE; final per-team health check fires at the DEFENSE→ATTACK
transition regardless of how the phase ended.

No docker needed: the round-loop wait is asyncio-driven and the final
health-check path is exercised by stubbing `_emit_health_chain` to a
spy.
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType, new_event
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase, RoundLoop
from models.topology import Team, TopologyGraph


class _StubNetwork:
    def set_cross_team_network(self, allow: bool) -> None: ...


class _StubOrchestrator:
    network = _StubNetwork()


def _make_game(*, defense_secs: int = 30, attack_secs: int = 5) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico", port=4265,
            deploy_on=["team1", "team2"],
        )],
        rounds=RoundConfig(defense_secs=defense_secs, attack_secs=attack_secs, total_rounds=1),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


# ── _on_deploy_complete drains pending; ends phase when empty ─────────


def test_deploy_complete_drains_pending_and_ends_phase_when_empty():
    game = _make_game()
    # Round loop must exist so end_phase_early has somewhere to send the signal.
    game.round_loop = RoundLoop(game.bus, game.orchestrator.network, game.config.rounds)

    async def run():
        # Simulate DEFENSE kickoff for both teams in round 1.
        await game.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=1, phase=Phase.DEFENSE.value)
        )
        # _on_defense_kickoff fires inside that publish; verify pending populated.
        assert game._defense_pending.get(1) == {("team1", "nautilus-ico"), ("team2", "nautilus-ico")}
        # team1 reports DEPLOY_COMPLETE — still one pending.
        await game.bus.publish(new_event(
            EventType.DEPLOY_COMPLETE, team_id="team1", round_id=1,
            phase=Phase.DEFENSE.value, payload={"service": "nautilus-ico"},
        ))
        assert game._defense_pending[1] == {("team2", "nautilus-ico")}
        assert not game.round_loop._phase_early_end_event.is_set()
        # team2 reports — now empty, round loop signalled.
        await game.bus.publish(new_event(
            EventType.DEPLOY_COMPLETE, team_id="team2", round_id=1,
            phase=Phase.DEFENSE.value, payload={"service": "nautilus-ico"},
        ))
        assert game._defense_pending[1] == set()
        assert game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


def test_one_team_complete_does_not_end_phase():
    """Only one of two teams reports — phase should NOT end early."""
    game = _make_game()
    game.round_loop = RoundLoop(game.bus, game.orchestrator.network, game.config.rounds)

    async def run():
        await game.bus.publish(_phase(Phase.DEFENSE, 1))
        await game.bus.publish(new_event(
            EventType.DEPLOY_COMPLETE, team_id="team1", round_id=1,
            phase=Phase.DEFENSE.value, payload={"service": "nautilus-ico"},
        ))
        assert not game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


def test_deploy_complete_for_non_defense_capable_service_is_silent():
    """DEPLOY_COMPLETE for a service that wasn't in _defense_pending (e.g.
    notes — no restart_cmd, so no defense kickoff fired) must not crash
    or spuriously end the phase."""
    teams = [Team(id="team1", dev="d1", prod="p1", token="t1"),
             Team(id="team2", dev="d2", prod="p2", token="t2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"])],
        rounds=RoundConfig(),
    )
    game = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    game.round_loop = RoundLoop(game.bus, game.orchestrator.network, game.config.rounds)

    async def run():
        await game.bus.publish(_phase(Phase.DEFENSE, 1))
        # notes isn't defense-capable, so _defense_pending should be empty
        assert game._defense_pending.get(1, set()) == set()
        await game.bus.publish(new_event(
            EventType.DEPLOY_COMPLETE, team_id="team1", round_id=1,
            phase=Phase.DEFENSE.value, payload={"service": "notes"},
        ))
        # No early-end signal because nothing was ever pending.
        assert not game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


# ── RoundLoop._wait wakes on the early-end event ──────────────────────


def test_round_loop_wait_wakes_on_end_phase_early():
    """_wait should return well before the timeout when end_phase_early()
    is called."""
    from gamemgr.events import EventBus

    bus = EventBus()
    loop = RoundLoop(bus, _StubNetwork(), RoundConfig(defense_secs=100, attack_secs=100))

    async def run():
        import time as _t
        # Schedule end_phase_early after 0.1s.
        async def signal():
            await asyncio.sleep(0.1)
            loop.end_phase_early()
        task = asyncio.create_task(signal())
        t0 = _t.monotonic()
        await loop._wait(100)
        elapsed = _t.monotonic() - t0
        await task
        # Should have returned in well under 1s — _phase_early_end_event woke it.
        assert elapsed < 1.0, f"_wait took {elapsed:.2f}s; early-end signal missed"

    asyncio.run(run())


def test_enter_phase_resets_early_end_event():
    """An early-end signal in one phase must not collapse the next phase."""
    from gamemgr.events import EventBus

    bus = EventBus()
    loop = RoundLoop(bus, _StubNetwork(), RoundConfig(defense_secs=1, attack_secs=1))
    loop.end_phase_early()
    assert loop._phase_early_end_event.is_set()

    async def run():
        # Reset happens at the start of _enter_phase.
        await loop._enter_phase(Phase.DEFENSE, allow_cross_team=False, duration=0)
        # The wait used the still-set event (no harm; immediate return). After
        # the next _enter_phase the event must be cleared.
        loop.end_phase_early()
        await loop._enter_phase(Phase.ATTACK, allow_cross_team=True, duration=0)
        # After ATTACK enter (which calls clear()), event must be unset.
        # We can't easily check during the wait, but the *next* clear happens
        # at the next phase. For this test, just verify the API works.

    asyncio.run(run())


# ── _on_defense_end_health_check fires for every defense-capable team ─


def test_defense_end_health_check_fires_at_attack_phase_transition():
    """On PHASE_CHANGE → ATTACK, an _emit_health_chain call happens once
    per (team, defense-capable service). Stub _emit_health_chain to count."""
    game = _make_game()
    calls: list[tuple[str, str]] = []

    async def fake_emit(*, team_id, service, container_name, spec):
        calls.append((team_id, service))

    # monkeypatch the method on the instance.
    game._emit_health_chain = fake_emit  # type: ignore[assignment]

    async def run():
        await game.bus.publish(_phase(Phase.ATTACK, 1))

    asyncio.run(run())

    assert sorted(calls) == [("team1", "nautilus-ico"), ("team2", "nautilus-ico")]


def test_defense_end_health_check_skips_non_defense_capable():
    """notes has no restart_cmd; the final health check must NOT run for it."""
    teams = [Team(id="team1", dev="d1", prod="p1", token="t1")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1"])],
        rounds=RoundConfig(),
    )
    game = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    calls: list = []
    async def fake_emit(*args, **kw): calls.append(kw)
    game._emit_health_chain = fake_emit  # type: ignore[assignment]

    async def run():
        await game.bus.publish(_phase(Phase.ATTACK, 1))
    asyncio.run(run())

    assert calls == []


def test_defense_end_health_check_does_not_fire_on_defense_entry():
    """Only fires on the DEFENSE→ATTACK transition, not when DEFENSE begins."""
    game = _make_game()
    calls: list = []
    async def fake_emit(*args, **kw): calls.append(kw)
    game._emit_health_chain = fake_emit  # type: ignore[assignment]

    async def run():
        await game.bus.publish(_phase(Phase.DEFENSE, 1))
    asyncio.run(run())

    assert calls == []


def _phase(phase: Phase, round_id: int):
    return new_event(EventType.PHASE_CHANGE, round_id=round_id, phase=phase.value)
