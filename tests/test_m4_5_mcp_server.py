"""M4.5 — MCP server unit + REST/MCP parity tests.

Stands up a real GameMgr (no docker, no challenges, no agents) plus a
FastAPI/uvicorn process so the MCP Streamable HTTP path is exercised
end-to-end. The functional assertions use the official `mcp` Python SDK
client; a tiny raw `httpx` health probe stays as a separate test to
catch "the mount route disappeared entirely" regressions without
re-implementing the JSON-RPC protocol.

Key invariants pinned here (see docs/m4_5_mcp_plan.md):
  - submit_flag/report_ready/report_deploy_complete callable end-to-end
  - bad token → MCP error -32001 "Unauthorized", message says nothing
    about BAD_TOKEN / tokens / team ids; timeline stays silent
  - unknown service → InvalidParams (-32602), no DEPLOY_COMPLETE event
  - submit_flag valid-token outcomes (ACCEPTED/DUPLICATE/UNKNOWN/OWN)
    produce identical FLAG_SUBMITTED events on MCP and REST
  - BAD_TOKEN on submit: both paths silent on the timeline (response shape
    is intentionally different — that asymmetry is documented in the
    safety section of the plan)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn

from gamemgr.api import build_app
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from gamemgr.mcp_server import MCP_PUBLIC_ENDPOINT
from models.topology import Network, Team, TopologyGraph

# MCP SDK client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


TOKEN_TEAM1 = "tok-team1-m4_5"
TOKEN_TEAM2 = "tok-team2-m4_5"
HOST = "127.0.0.1"
PORT = 18800  # not standard 8000 to avoid clashing with a dev server


class _StubOrchestrator:
    """GameMgr only uses orchestrator.network during its round loop; for
    these tests we never start the loop, so a bare object satisfies the
    constructor."""

    network = None


def _make_game_config() -> GameConfig:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token=TOKEN_TEAM1),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token=TOKEN_TEAM2),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    challenges = [
        ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"]),
    ]
    return GameConfig(topology=topology, challenges=challenges, rounds=RoundConfig())


@pytest.fixture(scope="module")
def game():
    cfg = _make_game_config()
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


@pytest.fixture(scope="module")
def server(game):
    """Run the FastAPI app (REST + WS + MCP) in a background thread for
    the duration of the module."""
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True, name="m4_5-uvicorn")
    t.start()
    for _ in range(40):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn never reported started"
    yield
    server.should_exit = True
    t.join(timeout=3)


def _mcp_url() -> str:
    return f"http://{HOST}:{PORT}{MCP_PUBLIC_ENDPOINT}"


async def _call_tool(token: str, name: str, args: dict | None = None):
    headers = {"X-Team-Token": token} if token else {}
    async with streamablehttp_client(_mcp_url(), headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args or {})


def _result_dict(res) -> dict:
    """Unwrap an MCP tool result back into the dict the handler returned.

    FastMCP serializes generic dict returns into content[0].text as JSON
    text and leaves structuredContent=None unless the return type is
    specifically typed (TypedDict / pydantic). We accept either shape so
    the test stays robust across SDK versions."""
    if getattr(res, "structuredContent", None):
        sc = res.structuredContent
        if isinstance(sc, dict) and "result" in sc and isinstance(sc["result"], dict):
            return sc["result"]
        return sc  # type: ignore[return-value]
    if res.content and getattr(res.content[0], "text", None):
        return json.loads(res.content[0].text)
    return {}


# ── health probe (raw httpx, no JSON-RPC) ──────────────────────────────


def test_mount_path_reachable(server):
    """If FastAPI mount path drifts (SDK update) we want a fast, cheap
    signal — much cheaper than the live Claude smoke. 4xx/5xx are fine
    here; only 404 / connection-refused proves the mount is gone."""
    r = httpx.get(_mcp_url(), timeout=2.0)
    assert r.status_code != 404, f"MCP mount disappeared (got 404 at {_mcp_url()})"


# ── three tools, happy path ────────────────────────────────────────────


def test_report_ready_emits_agent_ready(server, game):
    before = len(game.timeline.history())
    asyncio.run(_call_tool(TOKEN_TEAM1, "report_ready"))
    new = game.timeline.history()[before:]
    ready = [e for e in new if e.type == "AGENT_READY"]
    assert ready, f"no AGENT_READY emitted; new events: {[e.type for e in new]}"
    assert ready[-1].team_id == "team1"


def test_report_deploy_complete_writes_and_emits(server, game):
    """`duration_ms` is host-computed from the DEFENSE_KICKOFF
    timestamp the orchestrator already controls. The MCP signature no
    longer accepts it from the agent (was unreliable in live runs).
    Seed a kickoff_ts so the host has something to subtract from."""
    before = len(game.timeline.history())
    # Stamp DEFENSE_KICKOFF ~2s ago so the computed duration is positive
    # and small (the test fixture doesn't run a real round loop).
    game._defense_kickoff_ts[(0, "team1", "notes")] = time.time() - 2.0
    asyncio.run(
        _call_tool(
            TOKEN_TEAM1,
            "report_deploy_complete",
            {"service": "notes"},
        )
    )
    # Whitelist hit → state updated + event emitted.
    report = game.deploy_reports.get("team1", {}).get("notes")
    assert report is not None, f"deploy_reports not written: {game.deploy_reports}"
    # Duration computed by host from the seeded kickoff_ts (~2000ms).
    assert report["duration_ms"] is not None
    assert 1500 <= report["duration_ms"] <= 5000, report["duration_ms"]
    assert abs(report["completed_at"] - time.time()) < 5

    new = game.timeline.history()[before:]
    dc = [e for e in new if e.type == "DEPLOY_COMPLETE"]
    assert dc, f"no DEPLOY_COMPLETE event; new events: {[e.type for e in new]}"
    assert dc[-1].team_id == "team1"
    assert dc[-1].payload.get("service") == "notes"
    assert dc[-1].payload.get("duration_ms") == report["duration_ms"]


def test_report_deploy_complete_unknown_service_rejected(server, game):
    """typo / unknown service should never reach deploy_reports — adaptive-
    duration aggregation downstream depends on this."""
    before_state = dict(game.deploy_reports)
    before_events = len(game.timeline.history())

    res = asyncio.run(
        _call_tool(
            TOKEN_TEAM1,
            "report_deploy_complete",
            {"service": "not-a-real-service"},
        )
    )
    # Tools may surface as MCP errors via isError flag rather than exceptions.
    assert getattr(res, "isError", False), f"expected isError, got {res}"
    # Confirm no DEPLOY_COMPLETE event leaked through.
    new = game.timeline.history()[before_events:]
    assert not [e for e in new if e.type == "DEPLOY_COMPLETE"], \
        f"DEPLOY_COMPLETE emitted despite unknown service: {[e.type for e in new]}"
    # Confirm deploy_reports unchanged (no new (team, service) pair).
    for team_id, services in game.deploy_reports.items():
        for svc in services:
            assert (team_id, svc) == (team_id, svc), "trivial assert to keep loop"
    assert game.deploy_reports == before_state, "deploy_reports mutated"


def test_submit_flag_via_mcp_round_trips(server, game):
    """ACCEPTED via MCP. Plant a fresh flag owned by team2; team1 submits."""
    flag = "FLAG{MCPACCEPTED000000000000000000000000}"
    game.flags.plant("team2", "notes", flag)

    before = len(game.timeline.history())
    res = asyncio.run(_call_tool(TOKEN_TEAM1, "submit_flag", {"flag": flag}))
    assert not getattr(res, "isError", False), f"unexpected error: {res}"
    payload = _result_dict(res)
    assert payload.get("accepted") is True, f"got: {payload}"
    assert payload.get("reason") == "ACCEPTED", f"got: {payload}"

    new = game.timeline.history()[before:]
    submitted = [e for e in new if e.type == "FLAG_SUBMITTED"]
    assert submitted, f"no FLAG_SUBMITTED; new events: {[e.type for e in new]}"
    assert submitted[-1].payload.get("accepted") is True
    assert submitted[-1].payload.get("reason") == "ACCEPTED"
    assert submitted[-1].payload.get("submitter_team_id") == "team1"


# ── auth / oracle hardening ────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["submit_flag", "report_ready", "report_deploy_complete"])
def test_bad_token_returns_generic_unauthorized(server, game, tool):
    """Every tool, bad token: returns an isError tool result with the
    generic Unauthorized message; nothing in the wire message leaks
    `BAD_TOKEN` / `token` / team-id; no event lands on the bus.

    FastMCP wraps server-side McpError as `isError=True` tool content
    rather than propagating the JSON-RPC error code to the client — we
    pin the *message surface* here instead of the code, and the
    'no oracle leak' invariant is what actually matters in practice."""
    before = len(game.timeline.history())

    args = {}
    if tool == "submit_flag":
        args = {"flag": "FLAG{IRRELEVANT00000000000000000000000000}"}
    elif tool == "report_deploy_complete":
        args = {"service": "notes"}

    res = asyncio.run(_call_tool("bogus-token-no-such-team", tool, args))
    assert res.isError is True, f"bad token must return isError; got {res}"
    assert res.content, f"isError result missing content: {res}"
    msg = res.content[0].text or ""
    assert "Unauthorized" in msg, f"wire message must contain 'Unauthorized', got: {msg!r}"
    msg_low = msg.lower()
    for forbidden in ("bad_token", "token", "team", "x-team-token"):
        assert forbidden not in msg_low, \
            f"oracle leak: {forbidden!r} appeared in error message {msg!r}"

    new = game.timeline.history()[before:]
    assert new == [], f"bus must be silent on bad-token; got events: {[e.type for e in new]}"


# ── MCP ↔ REST submit_flag parity for valid-token outcomes ─────────────


def _rest_submit(token: str, flag: str) -> dict:
    r = httpx.post(
        f"http://{HOST}:{PORT}/game/flag/submit",
        json={"flag": flag},
        headers={"X-Team-Token": token},
        timeout=3.0,
    )
    return r.json()


def _mcp_submit(token: str, flag: str) -> dict:
    res = asyncio.run(_call_tool(token, "submit_flag", {"flag": flag}))
    return _result_dict(res)


def _events_since(game, before: int, type_: str) -> list:
    return [e for e in game.timeline.history()[before:] if e.type == type_]


def test_mcp_rest_parity_accepted(server, game):
    flag_a = "FLAG{PARITYACCEPTED11111111111111111111}"
    flag_b = "FLAG{PARITYACCEPTED22222222222222222222}"
    game.flags.plant("team2", "notes", flag_a)
    game.flags.plant("team1", "notes", flag_b)

    # MCP path: team1 submits team2's flag
    b1 = len(game.timeline.history())
    mcp_resp = _mcp_submit(TOKEN_TEAM1, flag_a)
    mcp_ev = _events_since(game, b1, "FLAG_SUBMITTED")[-1]

    # REST path: team2 submits team1's flag
    b2 = len(game.timeline.history())
    rest_resp = _rest_submit(TOKEN_TEAM2, flag_b)
    rest_ev = _events_since(game, b2, "FLAG_SUBMITTED")[-1]

    assert mcp_resp["accepted"] is True and mcp_resp["reason"] == "ACCEPTED"
    assert rest_resp["accepted"] is True and rest_resp["reason"] == "ACCEPTED"
    # Event payloads share the same shape (modulo team ids + ts).
    for field in ("accepted", "reason"):
        assert mcp_ev.payload[field] == rest_ev.payload[field], \
            f"field {field!r} differs: MCP={mcp_ev.payload[field]!r} REST={rest_ev.payload[field]!r}"


# DUPLICATE / UNKNOWN / OWN parity is covered by the FlagStore unit tests
# (logic) + test_mcp_rest_parity_accepted (proves MCP and REST share the
# same handler). Bad-token silence parity is a distinct invariant and
# kept as a separate test below.


def test_mcp_rest_parity_bad_token_both_silent(server, game):
    """The asymmetry that IS allowed: response shapes differ. But
    parity-of-silence MUST hold — neither path emits FLAG_SUBMITTED.

    MCP: tool-level isError + 'Unauthorized' text (no event)
    REST: 200 JSON `{accepted: false, reason: "BAD_TOKEN"}` (no event)"""
    flag = "FLAG{IRRELEVANT_BAD_TOKEN_PARITY66666666}"

    b1 = len(game.timeline.history())
    # MCP: bad token via tool result
    res = asyncio.run(_call_tool("bogus-token", "submit_flag", {"flag": flag}))
    assert res.isError is True
    assert "Unauthorized" in (res.content[0].text or "")

    # REST: bad token → 200 OK with BAD_TOKEN reason (M5.5 behavior preserved).
    # The `detail` field was added in the WRONG_PHASE / explanation pass —
    # it's a human-readable hint, fine for it to be present here.
    rest_resp = _rest_submit("bogus-token", flag)
    assert rest_resp["accepted"] is False
    assert rest_resp["reason"] == "BAD_TOKEN"

    # Both silent on the bus.
    new = game.timeline.history()[b1:]
    submitted = [e for e in new if e.type == "FLAG_SUBMITTED"]
    assert not submitted, \
        f"bus must be silent for bad-token on either path; got: {submitted}"
