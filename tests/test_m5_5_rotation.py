"""M5.5 — per-round flag rotation + per-team X-Team-Token auth.

Three groups:
  1. Config: Team.token round-trips through game.yaml
  2. Rotation: plant_flags(round_id) replaces flags; old flag goes UNKNOWN;
     the bus-subscriber wired in GameMgr.start() fires at every PHASE_CHANGE
     → DEFENSE
  3. HTTP auth: POST /game/flag/submit honors X-Team-Token and rejects
     missing / wrong tokens with BAD_TOKEN (no event emitted)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

from gamemgr.api import build_app
from gamemgr.config import GameConfig, RoundConfig, load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "game.yaml"


# ── config / dataclass round-trip ──────────────────────────────────────


def test_team_token_round_trips_through_yaml(tmp_path, monkeypatch):
    # game.yaml expander still demands API_ROUTER_* be set; conftest sets
    # placeholders, but make this test self-contained anyway.
    monkeypatch.setenv("API_ROUTER_BASE", "https://x")
    monkeypatch.setenv("API_ROUTER_KEY", "y")
    yaml_text = """
teams:
  - id: team1
    dev: t1d
    prod: t1p
    token: tok-1
  - id: team2
    dev: t2d
    prod: t2p
    # no token → defaults to ""
nodes: []
networks: {}
"""
    p = tmp_path / "game.yaml"
    p.write_text(yaml_text)
    cfg = load_game_config(p, env_file=None)
    by_id = {t.id: t for t in cfg.topology.teams}
    assert by_id["team1"].token == "tok-1"
    assert by_id["team2"].token == ""


def test_game_yaml_carries_real_tokens():
    cfg = load_game_config(CONFIG)
    tokens = {t.id: t.token for t in cfg.topology.teams}
    assert tokens == {"team1": "dev-team1-token", "team2": "dev-team2-token"}


# ── rotation, integration ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    try:
        c = docker.from_env()
        c.ping()
    except Exception as e:
        pytest.skip(f"docker daemon unavailable: {e}")
    return c


@pytest.fixture(scope="module")
def game(client):
    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    g = GameMgr(config=cfg, orchestrator=orch)
    orch.setup()
    g.deploy_challenges()
    for (team_id, _svc), challenge in g._challenge_by_owner.items():
        target = g.target_for(team_id, challenge)
        for _ in range(50):
            if challenge.functionality_test.run(target).passed:
                break
            time.sleep(0.1)
    g.plant_initial_flags()
    yield g
    g.teardown_challenges()
    orch.teardown()


def test_plant_flags_rotates(game):
    """plant_flags(round_id) should generate fresh flags and invalidate the
    prior ones — the FlagStore contract M5.5 leans on for rotation."""
    old = dict(game.flags.planted)
    assert old, "fixture should have planted initial flags"

    game.plant_flags(round_id=99)

    new = dict(game.flags.planted)
    assert set(old.keys()) == set(new.keys())
    for k in old:
        assert old[k] != new[k], f"flag for {k} did not change after rotation"

    # Every old flag is now UNKNOWN.
    for old_flag in old.values():
        r = game.flags.submit("any_team", old_flag)
        assert r.reason == "UNKNOWN", f"old flag still recognized: {old_flag}"

    # Every new flag is still claimable by a different team.
    for (owner, _service), new_flag in new.items():
        other = "team1" if owner == "team2" else "team2"
        r = game.flags.submit(other, new_flag)
        assert r.accepted is True


def test_round_loop_triggers_rotation_via_phase_change_subscriber(game, tmp_path):
    """Run a 2-round game loop on the shared arena fixture; assert the
    rotation subscriber fires once per DEFENSE entry.

    Reuses the module fixture to avoid double-tearing-down the team
    containers (the docker daemon is shared; spinning up a parallel
    Orchestrator on the same names would invalidate the fixture's state).
    """
    history_before = len(game.timeline.history())

    # Swap in fast RoundConfig just for this run, then restore.
    fast = RoundConfig(defense_secs=1, attack_secs=1, total_rounds=2)
    original_cfg = game.config
    game.config = GameConfig(
        topology=original_cfg.topology,
        challenges=original_cfg.challenges,
        rounds=fast,
        agents=original_cfg.agents,
    )

    # Clear FlagStore submissions so DUPLICATE state from earlier tests
    # doesn't taint anything (rotation tests don't inspect submissions but
    # later HTTP tests would).
    game.flags.submissions.clear()
    # Re-plant at round 0 so the fixture has a clean baseline.
    game.plant_initial_flags()

    try:
        asyncio.run(game.start(run_dir=tmp_path / "run"))
    finally:
        game.config = original_cfg

    new_events = game.timeline.history()[history_before:]
    planted_events = [e for e in new_events if e.type == "FLAG_PLANTED"]
    # 1 pre-game emission per (team, service) at round_id=0 (from start())
    #   + 1 rotation emission per (team, service) per round (2 rounds).
    # 2 teams × 1 service × (1 pre + 2 rounds) = 6 events.
    assert len(planted_events) == 6, (
        f"expected 6 FLAG_PLANTED events, got {len(planted_events)}: "
        f"{[(e.round_id, e.team_id) for e in planted_events]}"
    )
    rounds_planted = sorted({e.round_id for e in planted_events})
    assert rounds_planted == [0, 1, 2]
    for r in (0, 1, 2):
        teams_for_round = {e.team_id for e in planted_events if e.round_id == r}
        assert teams_for_round == {"team1", "team2"}


# ── HTTP auth ───────────────────────────────────────────────────────────


@pytest.fixture
def http_app(game):
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    return TestClient(app)


@pytest.fixture
def fresh_flag(game):
    """Re-plant before each token test so DUPLICATE state from a sibling
    test doesn't contaminate ACCEPTED expectations."""
    game.plant_flags(round_id=999)
    return game.flags.planted[("team1", "notes")]


def test_submit_with_valid_token_accepted(http_app, fresh_flag):
    r = http_app.post(
        "/game/flag/submit",
        headers={"X-Team-Token": "dev-team2-token"},
        json={"flag": fresh_flag},
    )
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "reason": "ACCEPTED"}


def test_submit_with_wrong_token_rejected(http_app, game, fresh_flag):
    before = sum(1 for e in game.timeline.history() if e.type == "FLAG_SUBMITTED")
    r = http_app.post(
        "/game/flag/submit",
        headers={"X-Team-Token": "not-a-real-token"},
        json={"flag": fresh_flag},
    )
    assert r.json() == {"accepted": False, "reason": "BAD_TOKEN"}
    after = sum(1 for e in game.timeline.history() if e.type == "FLAG_SUBMITTED")
    assert after == before, "BAD_TOKEN must not emit a FLAG_SUBMITTED event"


def test_submit_without_token_rejected(http_app, fresh_flag):
    r = http_app.post("/game/flag/submit", json={"flag": fresh_flag})
    assert r.json() == {"accepted": False, "reason": "BAD_TOKEN"}


def test_submit_body_team_id_is_ignored(http_app, fresh_flag):
    """Old M5-style body `team_id` is no longer authoritative. Even if the
    body says team1 (which would be OWN), the header derives team2 →
    ACCEPTED."""
    r = http_app.post(
        "/game/flag/submit",
        headers={"X-Team-Token": "dev-team2-token"},
        json={"team_id": "team1", "flag": fresh_flag},
    )
    assert r.json()["accepted"] is True, f"header should override body, got {r.json()}"
