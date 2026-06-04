"""DEFCON: ArtifactStore wiring + end-of-round service-log snapshots.

Per-phase agent transcripts (`stream-{phase}.jsonl` / `agent-{phase}.log`
/ `metadata-{phase}.json`) are written by `ClaudeAgentManager.run_phase`
directly. GameMgr only owns:
  - constructing ArtifactStore in start()
  - snapshotting `docker logs <team{N}_prod>` to service-logs/ via
    `on_round_stop`
"""

from __future__ import annotations

import asyncio

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.topology import Team, TopologyGraph
from orchestrator.handle import ContainerHandle


class _StubOrchestrator:
    network = None


def _make_game() -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico",
            port=4265, deploy_on=["team1", "team2"],
        )],
        rounds=RoundConfig(),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _stub_handle(name: str) -> ContainerHandle:
    return ContainerHandle(container_id=f"fake-{name}", name=name, networks=[])


# ── ArtifactStore is created in start() ───────────────────────────────


def test_start_constructs_artifact_store_in_run_dir(tmp_path):
    game = _make_game()
    assert game.artifacts is None

    async def run():
        async def _start_then_stop():
            await game.start(run_dir=tmp_path)
        task = asyncio.create_task(_start_then_stop())
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
    assert (tmp_path / "artifacts" / "README.md").is_file()


# ── on_round_stop: service logs from docker logs ──────────────────────


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
    """on_round_stop dumps `docker logs <team{N}_prod>` to service-logs/."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(game, "handle_for", lambda name: _stub_handle(name))
    monkeypatch.setattr(
        "gamemgr.game_mgr.docker.from_env",
        lambda: _StubDockerClient(),
    )

    async def run():
        await game.on_round_stop(round_id=3, phase=Phase.ATTACK.value)

    asyncio.run(run())

    for team in ("team1", "team2"):
        p = tmp_path / "artifacts" / team / "round-3" / "docker.log"
        assert p.is_file(), f"missing {p}"
        assert f"docker stdout for {team}_prod" in p.read_text()


def test_phase_change_does_not_write_service_logs(tmp_path, monkeypatch):
    """PHASE_CHANGE (any phase) is NOT a service-log snapshot point —
    only on_round_stop triggers `_persist_service_logs`."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(
        "gamemgr.game_mgr.docker.from_env",
        lambda: _StubDockerClient(),
    )

    # Drive several phase entries; none of them should write docker.log.
    game.enter_defense_phase(round_id=1)
    game.enter_attack_phase(round_id=1)

    assert list((tmp_path / "artifacts").rglob("docker.log")) == [], (
        "service-log snapshots must only happen on ROUND_STOP"
    )
