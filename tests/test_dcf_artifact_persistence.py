"""DEFCON: ArtifactStore wiring + end-of-round service-log snapshots.

Persistent-memory v1 split: per-phase agent transcripts
(`stream-{phase}.jsonl` / `agent-{phase}.log` / `metadata-{phase}.json`)
are written by `ClaudeAgentManager.run_phase` directly — see
test_dcf_run_phase.py for that path. GameMgr only owns:
  - constructing ArtifactStore in start()
  - snapshotting `docker logs <team{N}_prod>` to service-logs/ at ROUND_STOP

No docker for ArtifactStore construction. The service-log path is
exercised with a stub docker client (real docker is too heavy here).
"""

from __future__ import annotations

import asyncio

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType, new_event
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
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
    # exist immediately.
    assert (tmp_path / "artifacts" / "README.md").is_file()


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
    """ROUND_STOP dumps `docker logs <team{N}_prod>` to service-logs/."""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    monkeypatch.setattr(game, "handle_for", lambda name: _stub_handle(name))
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


# ── service-log persistence is ROUND_STOP-only ────────────────────────


def test_phase_change_does_not_write_service_logs(tmp_path, monkeypatch):
    """PHASE_CHANGE (any phase) is NOT a service-log snapshot point —
    only ROUND_STOP triggers `_persist_service_logs`. (Other PHASE_CHANGE
    subscribers like the defense-end health chain are allowed to touch
    docker; the contract here is just "no `docker.log` file appears".)"""
    game = _make_game()
    from gamemgr.artifacts import ArtifactStore
    game.artifacts = ArtifactStore(tmp_path)
    # Make sure if _persist_service_logs IS wrongly invoked it can still
    # complete without raising — we assert against the on-disk artifact,
    # not against a docker-touch counter.
    monkeypatch.setattr(
        "gamemgr.game_mgr.docker.from_env",
        lambda: _StubDockerClient(),
    )

    async def run():
        for p in (Phase.PREFLIGHT, Phase.SETUP, Phase.DEFENSE, Phase.ATTACK):
            await game.bus.publish(
                new_event(EventType.PHASE_CHANGE, round_id=1, phase=p.value)
            )
    asyncio.run(run())

    assert list((tmp_path / "artifacts").rglob("docker.log")) == [], (
        "service-log snapshots must only happen on ROUND_STOP"
    )
