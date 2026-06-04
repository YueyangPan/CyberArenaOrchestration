"""Unit test for GameMgr.target_for — the v2 helper that builds a
VulboxTarget for {team_id, challenge}.

Pure construction logic; no docker needed. The challenge instance is the
real NotesChallenge (cheap to construct) so we also pin the wiring
between Challenge.vulbox.exposed_ports and VulboxTarget.ports.
"""

from __future__ import annotations

import pytest

from challenges.interface import VulboxTarget
from challenges.notes import NotesChallenge
from gamemgr.config import GameConfig
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


def _minimal_cfg() -> GameConfig:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod"),
        Team(id="team2", dev="team2_dev", prod="team2_prod"),
    ]
    return GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[],
    )


class _FakeOrch:
    """GameMgr only touches `orchestrator` from start()/restart paths;
    target_for itself doesn't reach for it, so a sentinel is enough."""


def test_target_for_builds_privileged_target():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    target = g.target_for("team1", ch)

    assert isinstance(target, VulboxTarget)
    assert target.host == "team1_prod"
    # Ports are copied from the challenge's vulbox, NOT shared by ref.
    assert target.ports == {"service": 5000}
    assert target.ports is not ch.vulbox.exposed_ports
    assert target.meta["team_id"] == "team1"
    # exec_in_container is the bound method (not a wrapper) — handlers can
    # invoke it directly.
    assert target.meta["exec_in_container"] == g._exec_in_container


def test_target_for_different_teams_get_different_hosts():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    assert g.target_for("team1", ch).host == "team1_prod"
    assert g.target_for("team2", ch).host == "team2_prod"


def test_target_for_unknown_team_raises():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    with pytest.raises(KeyError, match="unknown team_id 'teamX'"):
        g.target_for("teamX", ch)
