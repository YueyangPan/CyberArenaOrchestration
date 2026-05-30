"""DEFCON: ArtifactStore wired into GameMgr's round-loop subscribers.

Pins the persistence pattern documented in the v1 plan §Artifact persistence:
- api-logs/   stdout from the agent's stream-json transcript per phase
- agent-logs/ stderr + exit_code per phase
- service-logs/ docker logs snapshot at end of round (real docker required)

No docker needed for the api-logs / agent-logs path — we mock
`_handle_for` + `fetch_agent_logs_handler` to short-circuit the docker
client. The service-logs path is exercised separately when docker is
present (skipped here when not).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType, new_event
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.agent import AgentLogs
from models.topology import Team, TopologyGraph
from orchestrator.service import ContainerHandle


class _StubOrchestrator:
    network = None


def _make_game(*, with_ico: bool = True) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    challenges = [
        ChallengeConfig(
            name="nautilus-ico" if with_ico else "notes",
            type="nautilus-ico" if with_ico else "notes",
            port=4265 if with_ico else 5000,
            deploy_on=["team1", "team2"],
        )
    ]
    cfg = GameConfig(topology=topology, challenges=challenges, rounds=RoundConfig())
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _stub_handle(name: str) -> ContainerHandle:
    """A ContainerHandle that doesn't touch docker."""
    return ContainerHandle(container_id=f"fake-{name}", name=name, networks=[])


# ── ArtifactStore is created in start() ───────────────────────────────


def test_start_constructs_artifact_store_in_run_dir(tmp_path):
    game = _make_game()
    assert game.artifacts is None  # not yet

    async def run():
        # Drive start() but stop the round loop immediately so we don't
        # block on the actual game. The round_loop check below will see
        # artifacts already populated.
        async def _start_then_stop():
            await game.start(run_dir=tmp_path)
        task = asyncio.create_task(_start_then_stop())
        # Give start() a tick to construct ArtifactStore + open the file sink.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await game.stop()
        try:
            await task
        except Exception:
            pass

    asyncio.run(run())

    assert game.artifacts is not None
    assert game.artifacts.root == tmp_path / "artifacts"
    # Per-team / round dirs are created lazily; only the root + README
    # exist immediately. (No `<kind>/` reservations in the new layout.)
    assert (tmp_path / "artifacts" / "README.md").is_file()


# ── PHASE_CHANGE → ATTACK: defense agent logs persisted ───────────────


def test_phase_change_to_attack_writes_defense_agent_logs(tmp_path, monkeypatch):
    """On PHASE_CHANGE → ATTACK, walk every dev container, fetch logs,
    write stdout → api-logs/<team>/round-N/stream-defense.jsonl, and
    stderr+exit_code → agent-logs/<team>/round-N/agent-defense.log.
    """
    game = _make_game()

    # Avoid the round loop entirely — just install the artifact store and
    # invoke the persistence subscriber directly.
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)

    # Skip docker: stub _handle_for so it returns a fake handle.
    monkeypatch.setattr(game, "_handle_for", lambda name: _stub_handle(name))

    # Canned fetch handler — different content per team so we can assert routing.
    def fake_fetch(handle: ContainerHandle) -> AgentLogs:
        team_num = handle.name.split("_")[0]  # "team1" / "team2"
        return AgentLogs(
            stdout=f'{{"type":"text","content":"hello from {team_num}"}}\n',
            stderr=f"some warning on {team_num}\n",
            exit_code=0,
        )

    game.register_fetch_agent_logs_handler(fake_fetch)

    async def run():
        await game.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=1, phase=Phase.ATTACK.value)
        )

    asyncio.run(run())

    # Both teams' defense logs should exist under <team>/round-1/.
    for team in ("team1", "team2"):
        api_p = tmp_path / "artifacts" / team / "round-1" / "stream-defense.jsonl"
        assert api_p.is_file(), f"missing {api_p}"
        assert f"hello from {team}" in api_p.read_text()

        agent_p = tmp_path / "artifacts" / team / "round-1" / "agent-defense.log"
        assert agent_p.is_file(), f"missing {agent_p}"
        body = agent_p.read_text()
        assert "exit_code=0" in body
        assert f"some warning on {team}" in body


def test_round_stop_writes_attack_agent_logs(tmp_path, monkeypatch):
    """On ROUND_STOP, persist attack-phase agent logs."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(game, "_handle_for", lambda name: _stub_handle(name))
    monkeypatch.setattr(
        "gamemgr.game_mgr.docker.from_env",
        lambda: _StubDockerClient(),
    )

    def fake_fetch(handle):
        return AgentLogs(stdout="attack-stream-content\n", stderr="", exit_code=0)
    game.register_fetch_agent_logs_handler(fake_fetch)

    async def run():
        await game.bus.publish(
            new_event(EventType.ROUND_STOP, round_id=2, phase=Phase.ATTACK.value)
        )
    asyncio.run(run())

    for team in ("team1", "team2"):
        api_p = tmp_path / "artifacts" / team / "round-2" / "stream-attack.jsonl"
        assert api_p.is_file(), f"missing {api_p}"
        assert "attack-stream-content" in api_p.read_text()
        agent_p = tmp_path / "artifacts" / team / "round-2" / "agent-attack.log"
        assert agent_p.is_file()


# ── ROUND_STOP: service logs from docker logs ─────────────────────────


class _StubContainer:
    def __init__(self, name: str):
        self._name = name
    def logs(self, *, timestamps: bool = False) -> bytes:
        return f"docker stdout for {self._name}\n".encode()


class _StubDockerClient:
    class _Containers:
        def get(self, name: str) -> _StubContainer:
            return _StubContainer(name)
    def __init__(self):
        self.containers = self._Containers()


def test_round_stop_writes_service_logs_via_docker_logs(tmp_path, monkeypatch):
    """ROUND_STOP also dumps `docker logs <team{N}_prod>` to service-logs/."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(game, "_handle_for", lambda name: _stub_handle(name))
    # Mock docker.from_env() (used inside _persist_service_logs).
    monkeypatch.setattr(
        "gamemgr.game_mgr.docker.from_env",
        lambda: _StubDockerClient(),
    )

    async def run():
        await game.bus.publish(
            new_event(EventType.ROUND_STOP, round_id=3, phase=Phase.ATTACK.value)
        )
    asyncio.run(run())

    for team in ("team1", "team2"):
        p = tmp_path / "artifacts" / team / "round-3" / "docker.log"
        assert p.is_file(), f"missing {p}"
        assert f"docker stdout for {team}_prod" in p.read_text()


# ── DEFENSE phase entry MUST NOT clobber attack data ─────────────────


def test_defense_phase_entry_does_not_write_artifacts(tmp_path, monkeypatch):
    """PHASE_CHANGE → DEFENSE is not a snapshot point — only → ATTACK and ROUND_STOP are."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(game, "_handle_for", lambda name: _stub_handle(name))

    fetched: list = []
    def fake_fetch(handle):
        fetched.append(handle.name)
        return AgentLogs()
    game.register_fetch_agent_logs_handler(fake_fetch)

    async def run():
        await game.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=1, phase=Phase.DEFENSE.value)
        )
    asyncio.run(run())

    assert fetched == [], "fetch should NOT happen on DEFENSE entry"
    # team1/round-1 dir should NOT exist (no file written).
    assert not (tmp_path / "artifacts" / "team1" / "round-1").exists()


# ── No handler registered: subscriber no-ops cleanly ─────────────────


def test_persist_no_handler_silently_skips_agent_logs(tmp_path):
    """Without `register_fetch_agent_logs_handler`, api-logs/agent-logs paths
    don't get written — but service-logs still try (they go through docker)."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    # No fetch handler registered.

    async def run():
        await game.bus.publish(
            new_event(EventType.PHASE_CHANGE, round_id=1, phase=Phase.ATTACK.value)
        )
    asyncio.run(run())

    # No agent stream / stderr files anywhere (only docker.log can land,
    # but no service subscriber fired in this scenario either).
    assert list((tmp_path / "artifacts").rglob("stream-*.jsonl")) == []
    assert list((tmp_path / "artifacts").rglob("agent-*.log")) == []
