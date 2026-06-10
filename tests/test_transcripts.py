"""Tests for agent stream-json transcript parsing + the /game/agents
endpoint that feeds the panel's transcript section."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from gamemgr.transcripts import collect_transcripts, parse_stream_json, summary_stats
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "game.yaml"


# ── sample stream-json ─────────────────────────────────────────────────

_SYSTEM = {"type": "system", "subtype": "init", "model": "claude-x", "tools": ["Bash", "Read"]}
_ASSISTANT = {
    "type": "assistant",
    "message": {
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "content": [
            {"type": "thinking", "thinking": "let me look around"},
            {"type": "text", "text": "I'll list the files."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "list dir"}},
        ],
    },
}
_TOOL_OK = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "file1\nfile2", "is_error": False},
]}}
_TOOL_ERR = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": [{"type": "text", "text": "boom"}], "is_error": True},
]}}


def _write_stream(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n")


# ── parse_stream_json ──────────────────────────────────────────────────


def test_parse_unpacks_each_block_in_order(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT, _TOOL_OK, _TOOL_ERR])
    events = parse_stream_json(p)

    kinds = [e.kind for e in events]
    assert kinds == ["system", "thinking", "text", "tool_use", "tool_result", "tool_result"]

    tool_use = events[3]
    assert tool_use.tool_name == "Bash"
    assert "list dir" in tool_use.title

    # Per-turn usage is attached to the turn's first event only (here the
    # thinking block), so summing across events can't multiply-count it.
    assert events[1].kind == "thinking"
    assert events[1].tokens_in == 100 and events[1].tokens_out == 20
    assert tool_use.tokens_in is None and tool_use.tokens_out is None

    assert events[4].is_error is False
    assert events[5].is_error is True
    assert events[5].title.startswith("ERR ")
    assert "boom" in events[5].body


def test_parse_skips_malformed_and_blank_lines(tmp_path):
    p = tmp_path / "stream-attack.jsonl"
    p.write_text("\n".join([json.dumps(_SYSTEM), "not json {", "", json.dumps(_TOOL_OK)]) + "\n")
    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["system", "tool_result"]


def test_parse_missing_file_returns_empty(tmp_path):
    assert parse_stream_json(tmp_path / "nope.jsonl") == []


# ── summary_stats ──────────────────────────────────────────────────────


def test_summary_stats_counts_tools_tokens_errors(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT, _TOOL_OK, _TOOL_ERR])
    stats = summary_stats(parse_stream_json(p))
    assert stats["events"] == 6
    assert stats["tools"] == 1
    assert "Bash" in stats["tools_breakdown"]
    assert stats["errors"] == 1
    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 20


# ── collect_transcripts ────────────────────────────────────────────────


def test_collect_groups_and_sorts_by_team_round_phase(tmp_path):
    arts = tmp_path / "artifacts"
    _write_stream(arts / "team2" / "round-1" / "stream-attack.jsonl", [_SYSTEM, _ASSISTANT])
    _write_stream(arts / "team1" / "round-1" / "stream-attack.jsonl", [_TOOL_OK])
    _write_stream(arts / "team1" / "round-1" / "stream-defense.jsonl", [_SYSTEM])
    _write_stream(arts / "team1" / "round-2" / "stream-attack.jsonl", [_TOOL_OK])

    out = collect_transcripts(tmp_path)
    # Chronological within a round: defense before attack (NOT alphabetical,
    # which would put R1 attack ahead of R1 defense).
    assert [(t["team_id"], t["round_id"], t["phase"]) for t in out] == [
        ("team1", 1, "defense"),
        ("team1", 1, "attack"),
        ("team1", 2, "attack"),
        ("team2", 1, "attack"),
    ]
    assert out[0]["stats"]["events"] == 1
    assert isinstance(out[0]["events"], list)


def test_collect_skips_empty_transcripts(tmp_path):
    arts = tmp_path / "artifacts"
    (arts / "team1" / "round-1").mkdir(parents=True)
    (arts / "team1" / "round-1" / "stream-defense.jsonl").write_text("")
    assert collect_transcripts(tmp_path) == []


def test_collect_no_artifacts_dir_returns_empty(tmp_path):
    assert collect_transcripts(tmp_path) == []


# ── /game/agents endpoint ──────────────────────────────────────────────


@pytest.fixture
def app_client():
    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    shutdown = asyncio.Event()
    return game, TestClient(build_app(game, shutdown))


def test_agents_endpoint_empty_before_run(app_client):
    game, client = app_client
    assert game.run_dir is None
    r = client.get("/game/agents")
    assert r.status_code == 200
    assert r.json() == {"run": None, "transcripts": []}


def test_agents_endpoint_serves_parsed_transcripts(app_client, tmp_path):
    game, client = app_client
    _write_stream(
        tmp_path / "artifacts" / "team1" / "round-1" / "stream-defense.jsonl",
        [_SYSTEM, _ASSISTANT, _TOOL_OK],
    )
    game.run_dir = tmp_path  # simulate a started run

    body = client.get("/game/agents").json()
    assert body["run"] == tmp_path.name
    assert len(body["transcripts"]) == 1
    tr = body["transcripts"][0]
    assert (tr["team_id"], tr["round_id"], tr["phase"]) == ("team1", 1, "defense")
    assert tr["stats"]["tools"] == 1
    assert [e["kind"] for e in tr["events"]][:2] == ["system", "thinking"]
