"""HealthPoller unit tests.

Verify:
  - on_attack_enter fires one immediate probe per target
  - periodic loop runs at the configured interval
  - on_attack_exit cancels all periodic tasks
  - trigger_now runs one-shot for a specific (team, service)
  - non-defense-capable services have no target (trigger_now no-ops)
"""

from __future__ import annotations

import asyncio

import pytest

from challenges.source import ChallengeSpec, FlagMechanism
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from health.checker import HealthChecker
from health.poller import DEFAULT_INTERVAL_SECS, HealthPoller, PollTarget, build_targets


def _spec(*, restart=True, interval=None) -> ChallengeSpec:
    md = {"flag_path": "/flag", "health_check": {}}
    if restart:
        md["restart_cmd"] = "echo"
    if interval is not None:
        md["health_check"]["interval_secs"] = interval
    return ChallengeSpec(
        name="nautilus-ico", source="nautilus", port=4265,
        image_tag="x", flag_mechanism=FlagMechanism.FILE_AT_PATH,
        metadata=md,
    )


def _checker(timeline: Timeline) -> HealthChecker:
    return HealthChecker(
        timeline,
        exec_in_container=lambda c, cmd: (0, "ok"),
        service_resolver=lambda t, s: None,
        handle_resolver=lambda n: None,
        status_resolver=lambda: {"round": 1, "phase": "ATTACK"},
    )


def _checks(timeline: Timeline) -> list:
    return [e for e in timeline.history() if e.type == EventType.SERVICE_HEALTH_CHECK.value]


def _target(team: str, *, interval: float) -> PollTarget:
    return PollTarget(
        team_id=team, service="nautilus-ico", container_name=f"{team}_prod",
        spec=_spec(), interval_secs=interval,
    )


# ── on_attack_enter ────────────────────────────────────────────────────


def test_on_attack_enter_fires_immediate_probe_per_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100), _target("team2", interval=100)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        # Each target gets a 4-level chain (4 events). Wait for immediate
        # probes to finish.
        await asyncio.gather(*immediate)
        # Stop the periodic tasks before they fire.
        poller.on_attack_exit()

    asyncio.run(run())

    by_team = {}
    for e in _checks(tl):
        by_team.setdefault(e.team_id, []).append(e.payload["level"])
    assert sorted(by_team.keys()) == ["team1", "team2"]
    assert len(by_team["team1"]) == 4
    assert len(by_team["team2"]) == 4


def test_periodic_loop_runs_at_interval():
    """With interval=0.05s, ~3 ticks land in 0.2s. Allow slack — we just
    need to see more than one round of probes per target."""
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=0.05)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        await asyncio.gather(*immediate)
        # 0.2s = ~3 periodic ticks after the immediate.
        await asyncio.sleep(0.2)
        poller.on_attack_exit()

    asyncio.run(run())

    # Immediate + at least 2 periodic = ≥ 12 SERVICE_HEALTH_CHECK events.
    # Use a soft lower bound to tolerate timing jitter.
    assert len(_checks(tl)) >= 8, f"expected ≥8 checks, got {len(_checks(tl))}"


def test_on_attack_exit_cancels_periodic_tasks():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=0.02)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        await asyncio.gather(*immediate)
        # Stop immediately, then sleep — no further probes should land.
        poller.on_attack_exit()
        baseline = len(_checks(tl))
        await asyncio.sleep(0.1)
        assert len(_checks(tl)) == baseline

    asyncio.run(run())


# ── trigger_now ────────────────────────────────────────────────────────


def test_trigger_now_runs_one_chain_for_known_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100)])

    async def run():
        await poller.trigger_now("team1", "nautilus-ico")

    asyncio.run(run())
    assert len(_checks(tl)) == 4


def test_trigger_now_is_noop_for_unknown_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100)])

    async def run():
        await poller.trigger_now("team-bogus", "nautilus-ico")
        await poller.trigger_now("team1", "not-a-service")

    asyncio.run(run())
    assert _checks(tl) == []


# ── build_targets cross-product + filter ──────────────────────────────


def test_build_targets_filters_non_defense_capable():
    """Services without restart_cmd are not pollable."""
    from gamemgr.config import ChallengeConfig
    from models.topology import Team

    challenges = [
        ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"]),
        ChallengeConfig(name="nautilus-ico", type="nautilus-ico", port=4265, deploy_on=["team1", "team2"]),
    ]
    teams_by_id = {
        "team1": Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
        "team2": Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
    }
    specs = {
        "notes": _spec(restart=False),                  # no restart_cmd
        "nautilus-ico": _spec(restart=True, interval=5),
    }
    targets = build_targets(
        challenges=challenges,
        teams_by_id=teams_by_id,
        resolve_spec=specs.get,
    )
    # notes filtered out; nautilus-ico × 2 teams = 2 targets.
    assert len(targets) == 2
    assert all(t.service == "nautilus-ico" for t in targets)
    assert sorted(t.team_id for t in targets) == ["team1", "team2"]
    assert all(t.interval_secs == 5.0 for t in targets)


def test_build_targets_falls_back_to_default_interval(caplog):
    from gamemgr.config import ChallengeConfig
    from models.topology import Team

    challenges = [
        ChallengeConfig(name="nautilus-ico", type="nautilus-ico", port=4265, deploy_on=["team1"]),
    ]
    teams_by_id = {"team1": Team(id="team1", dev="d", prod="p", token="t")}
    specs = {"nautilus-ico": _spec(restart=True, interval=None)}  # no interval_secs
    targets = build_targets(
        challenges=challenges,
        teams_by_id=teams_by_id,
        resolve_spec=specs.get,
    )
    assert len(targets) == 1
    assert targets[0].interval_secs == float(DEFAULT_INTERVAL_SECS)
    assert any("interval_secs" in rec.message for rec in caplog.records)


def test_empty_targets_list_makes_poller_noop():
    """notes-only game (no defense-capable services) — poller doesn't crash."""
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        assert immediate == []
        poller.on_attack_exit()
        await poller.stop()

    asyncio.run(run())
    assert _checks(tl) == []
