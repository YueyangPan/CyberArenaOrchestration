"""M5 — flag plant + submit verification.

Unit tests on FlagStore + integration test that:
  1. plant_initial_flags() puts a real `FLAG{...}` on each prod's notes service
  2. an attacker pivoting through their own prod can read the opposing flag
     over game_net during ATTACK (and cannot during DEFENSE — M3 invariant)
  3. submit_flag() classifies ACCEPTED / OWN / DUPLICATE / UNKNOWN correctly
  4. each submission emits a FLAG_SUBMITTED event whose payload never carries
     the flag string itself
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import docker
import pytest

from gamemgr.config import load as load_game_config
from gamemgr.flag import FlagStore, SubmitResult, new_flag
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "game.yaml"
FLAG_RE = re.compile(r"FLAG\{[A-Z0-9]{32}\}")


# ── unit tests: FlagStore ──────────────────────────────────────────────


def test_new_flag_shape():
    for _ in range(20):
        f = new_flag()
        assert FLAG_RE.fullmatch(f), f"bad shape: {f!r}"


def test_submit_accepts_then_rejects_duplicate():
    fs = FlagStore()
    fs.plant("team1", "notes", "FLAG{A}")
    r1 = fs.submit("team2", "FLAG{A}")
    assert r1 == SubmitResult(accepted=True, reason="ACCEPTED",
                              victim_team_id="team1", service="notes")
    r2 = fs.submit("team2", "FLAG{A}")
    assert r2.accepted is False and r2.reason == "DUPLICATE"


def test_submit_rejects_own_flag():
    fs = FlagStore()
    fs.plant("team1", "notes", "FLAG{B}")
    r = fs.submit("team1", "FLAG{B}")
    assert r.accepted is False and r.reason == "OWN"


def test_submit_rejects_unknown_flag():
    fs = FlagStore()
    fs.plant("team1", "notes", "FLAG{C}")
    r = fs.submit("team2", "FLAG{NEVER_PLANTED}")
    assert r.accepted is False and r.reason == "UNKNOWN"


def test_replant_invalidates_old_flag():
    """Re-planting on the same (team, service) drops the prior flag from the
    inverted index so it can no longer be submitted. Foundation for M5.5
    per-round rotation."""
    fs = FlagStore()
    fs.plant("team1", "notes", "FLAG{OLD}")
    fs.plant("team1", "notes", "FLAG{NEW}")
    assert fs.submit("team2", "FLAG{OLD}").reason == "UNKNOWN"
    assert fs.submit("team2", "FLAG{NEW}").reason == "ACCEPTED"


def test_submit_different_victims_independent():
    fs = FlagStore()
    fs.plant("team1", "notes", "FLAG{A}")
    fs.plant("team2", "notes", "FLAG{B}")
    # team3 stealing both is fine — different (victim, service) keys
    assert fs.submit("team3", "FLAG{A}").reason == "ACCEPTED"
    assert fs.submit("team3", "FLAG{B}").reason == "ACCEPTED"


# ── integration: real docker, real flag chain ──────────────────────────


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

    # wait for notes to come up on every prod (port-level + protocol-level)
    for service, handles in g._deployed:
        for h in handles:
            for _ in range(50):
                if service.is_alive(h):
                    break
                time.sleep(0.1)

    g.plant_initial_flags()

    # leave cross-team OPEN (attack-equivalent) so tests can steal flags
    orch.network.set_cross_team_network(True)

    yield g

    orch.network.set_cross_team_network(True)
    g.teardown_challenges()
    orch.teardown()


def _steal_via_socket(client, source: str, target: str, port: int) -> str | None:
    """Walk the notes store from inside `source` looking for a flag string.

    Reads from newest to oldest, returns the first FLAG{...} encountered.
    Mirrors the attack pattern the agent would run from its own prod.
    """
    probe = (
        f"import socket\n"
        f"def cmd(c):\n"
        f"    s = socket.socket(); s.settimeout(3)\n"
        f"    s.connect(('{target}', {port}))\n"
        f"    s.sendall(c.encode() + b'\\n')\n"
        f"    data = b''\n"
        f"    while not data.endswith(b'\\n'):\n"
        f"        chunk = s.recv(4096)\n"
        f"        if not chunk: break\n"
        f"        data += chunk\n"
        f"    return data.decode('utf8', 'replace').strip()\n"
        f"resp = cmd('LIST')\n"
        f"assert resp.startswith('COUNT '), resp\n"
        f"n = int(resp.split()[1])\n"
        f"for i in range(n - 1, -1, -1):\n"
        f"    val = cmd(f'GET {{i}}')\n"
        f"    if val.startswith('FLAG{{') and val.endswith('}}'):\n"
        f"        print(val); break\n"
    )
    r = client.containers.get(source).exec_run(["python3", "-c", probe])
    out = (r.output or b"").decode(errors="replace").strip()
    if r.exit_code != 0 or not out:
        return None
    return out


def test_plant_populates_flagstore(game):
    """plant_initial_flags() filled FlagStore with one flag per (team, service)."""
    keys = set(game.flags.planted.keys())
    assert keys == {("team1", "notes"), ("team2", "notes")}
    for flag in game.flags.planted.values():
        assert FLAG_RE.fullmatch(flag)


def test_flag_actually_present_on_prod(client, game):
    """The planted flag is retrievable from inside the prod container."""
    t1_flag = game.flags.planted[("team1", "notes")]
    found = _steal_via_socket(client, "team1_prod", "127.0.0.1", 5000)
    assert found == t1_flag


def test_cross_team_steal_via_prod_pivot(client, game):
    """team2's attacker (its own prod, on game_net) reaches team1_prod and steals.

    Mirrors the design's threat model: agents in DEV pivot through their
    own PROD container (only place on game_net) to attack opposing PRODs.
    """
    t1_flag = game.flags.planted[("team1", "notes")]
    stolen = _steal_via_socket(client, "team2_prod", "team1_prod", 5000)
    assert stolen == t1_flag


def test_submit_accepted_emits_event(game):
    """Stealing then submitting a foreign flag yields ACCEPTED + event with no flag leak."""
    t1_flag = game.flags.planted[("team1", "notes")]
    history_before = len(game.bus.history())

    result = asyncio.run(game.submit_flag("team2", t1_flag))
    assert result.accepted is True
    assert result.reason == "ACCEPTED"
    assert result.victim_team_id == "team1"
    assert result.service == "notes"

    new_events = game.bus.history()[history_before:]
    submitted = [e for e in new_events if e.type == "FLAG_SUBMITTED"]
    assert len(submitted) == 1, f"expected exactly 1 FLAG_SUBMITTED, got {len(submitted)}"
    ev = submitted[0]
    assert ev.payload["accepted"] is True
    assert ev.payload["reason"] == "ACCEPTED"
    assert ev.payload["submitter_team_id"] == "team2"
    assert ev.payload["victim_team_id"] == "team1"
    assert ev.payload["service"] == "notes"
    # The flag string must NOT appear anywhere in the event payload.
    flat = " ".join(str(v) for v in ev.payload.values())
    assert t1_flag not in flat, "flag string leaked into FLAG_SUBMITTED payload"


def test_submit_own_flag_rejected(game):
    """team1 trying to submit its own flag → OWN."""
    t1_flag = game.flags.planted[("team1", "notes")]
    r = asyncio.run(game.submit_flag("team1", t1_flag))
    assert r.accepted is False
    assert r.reason == "OWN"


def test_submit_unknown_flag_rejected(game):
    r = asyncio.run(game.submit_flag("team2", "FLAG{ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ}"))
    assert r.accepted is False
    assert r.reason == "UNKNOWN"


def test_submit_duplicate_rejected(game):
    """Second submission of the same (victim, service) by the same team → DUPLICATE.

    Runs after test_submit_accepted_emits_event has already submitted team1's
    flag from team2 (module-scoped fixture preserves FlagStore state across
    tests in this file).
    """
    t1_flag = game.flags.planted[("team1", "notes")]
    r = asyncio.run(game.submit_flag("team2", t1_flag))
    assert r.accepted is False
    assert r.reason == "DUPLICATE"
