"""Async mode: config parsing, AsyncLoop side effects, agent supervisor,
status fields, and LIVE-phase gate behavior.

Offline — no Docker, no real agents. AsyncLoop is driven against fakes and the
agent supervisor loop (`_run_async_agent`) is exercised with a fake manager.
"""

from __future__ import annotations

import asyncio

import pytest

import main
from gamemgr.async_loop import AsyncLoop
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.config import load as load_config
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _agents(*team_ids: str) -> dict[str, AgentConfig]:
    return {
        tid: AgentConfig(
            cli="claude --print", model="claude-sonnet-4-5",
            api_key="test-key", base_url="https://api.anthropic.com",
        )
        for tid in team_ids
    }


def _make_game(
    *,
    challenges: list[ChallengeConfig] | None = None,
    agents: dict[str, AgentConfig] | None = None,
    mode: str = "async",
    round_secs: int = 0,
    total_rounds: int = 3,
) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=challenges or [],
        rounds=RoundConfig(mode=mode, round_secs=round_secs, total_rounds=total_rounds),
        agents=agents or {},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


# ── config parsing / validation ────────────────────────────────────────


def _write_cfg(tmp_path, body: str):
    p = tmp_path / "game.yaml"
    p.write_text(body)
    return p


def test_config_defaults_to_sync(tmp_path):
    p = _write_cfg(tmp_path, "teams: [{id: t1, dev: t1d, prod: t1p}]\n")
    cfg = load_config(p, env_file=None)
    assert cfg.rounds.mode == "sync"
    assert cfg.rounds.round_secs == 60


def test_config_async_parsed(tmp_path):
    p = _write_cfg(
        tmp_path,
        "teams: [{id: t1, dev: t1d, prod: t1p}]\n"
        "round_config: {mode: async, round_secs: 45, total_rounds: 5}\n",
    )
    cfg = load_config(p, env_file=None)
    assert cfg.rounds.mode == "async"
    assert cfg.rounds.round_secs == 45
    assert cfg.rounds.total_rounds == 5


def test_config_invalid_mode_rejected(tmp_path):
    p = _write_cfg(
        tmp_path,
        "teams: [{id: t1, dev: t1d, prod: t1p}]\n"
        "round_config: {mode: turbo}\n",
    )
    with pytest.raises(ValueError, match="mode"):
        load_config(p, env_file=None)


# ── AsyncLoop side effects ──────────────────────────────────────────────


class _FakeNetwork:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_cross_team_network(self, allow: bool) -> None:
        self.calls.append(allow)


class _FakeHealthPoller:
    def __init__(self) -> None:
        self.enters: list[int] = []
        self.exits = 0
        self.stopped = 0

    def on_attack_enter(self, round_id: int) -> None:
        self.enters.append(round_id)

    def on_attack_exit(self) -> None:
        self.exits += 1

    async def stop(self) -> None:
        self.stopped += 1


def test_async_loop_side_effects():
    """Networks ON exactly once (never OFF); flags rotated per round; health
    polling started once; no DEFENSE/ATTACK phase changes; LIVE→STOPPED;
    _game_over set; ROUND_START per tick + GAME_END."""
    game = _make_game(total_rounds=3)
    net = _FakeNetwork()
    poller = _FakeHealthPoller()
    game.health_poller = poller

    rotated: list[int] = []
    game.flag_server.rotate = lambda rid: rotated.append(rid)  # type: ignore[assignment]

    loop = AsyncLoop(game, net, game.config.rounds)
    asyncio.run(loop.run())

    assert net.calls == [True]                # connected once, never disconnected
    assert poller.enters == [0]               # continuous polling started once
    assert poller.exits == 0                  # never exited mid-game
    assert rotated == [1, 2, 3]               # one rotation per round tick
    assert loop.phase == Phase.STOPPED
    assert game.is_game_over()

    history = [e.type for e in game.timeline.history()]
    assert history.count("ROUND_START") == 3
    assert "GAME_END" in history
    # AsyncLoop must NOT drive the sync per-phase machinery.
    assert "PHASE_CHANGE" not in history


def test_async_loop_stops_early_on_stop():
    game = _make_game(total_rounds=100, round_secs=30)
    net = _FakeNetwork()
    game.flag_server.rotate = lambda rid: None  # type: ignore[assignment]

    loop = AsyncLoop(game, net, game.config.rounds)

    async def run():
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0)        # let it reach the first _wait
        loop.stop()
        await task

    asyncio.run(run())
    assert loop.phase == Phase.STOPPED
    assert game.is_game_over()
    assert net.calls == [True]


# ── start_async_agents ──────────────────────────────────────────────────


def test_start_async_agents_fires_one_supervisor_per_team():
    game = _make_game(
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1", "team2"])],
        agents=_agents("team1", "team2"),
    )
    seen: list[str] = []

    async def supervisor(team_id, cfg):
        seen.append(team_id)
        # combined briefing carries both halves
        assert "ATTACK PLAYBOOK" in cfg.system_prompt
        assert "DEFENSE PLAYBOOK" in cfg.system_prompt

    game.register_async_supervisor_handler(supervisor)

    async def run():
        tasks = game.start_async_agents()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert sorted(seen) == ["team1", "team2"]


def test_async_prompt_drops_defense_for_attack_only_service():
    """notes is attack-only (no restart_handler) → defense half collapses to
    the no-op line, and there's no WRONG_PHASE language anywhere."""
    game = _make_game(
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1"])],
        agents=_agents("team1"),
    )
    cfg = game.async_agent_config_for("team1")
    # No phase-gate defense language (the generic defense recipe's
    # "DO NOT SUBMIT FLAGS" block must not leak into async).
    assert "DO NOT SUBMIT" not in cfg.system_prompt
    assert "No defendable service" in cfg.system_prompt


def test_async_prompt_folds_per_challenge_defense_for_defendable_service():
    """A defendable challenge (nautilus-ico) folds its RICH per-challenge
    defense content (source layout + vuln hotspots) into the combined
    briefing, minus the phase-gate language."""
    game = _make_game(
        challenges=[ChallengeConfig(name="nautilus-ico", type="nautilus-ico",
                                    port=4265, deploy_on=["team1"])],
        agents=_agents("team1"),
    )
    p = game.async_agent_config_for("team1").system_prompt
    assert "ATTACK PLAYBOOK" in p and "DEFENSE PLAYBOOK" in p
    assert "DO NOT SUBMIT" not in p              # phase gate dropped for async
    assert "LIKELY VULN HOTSPOTS" in p           # per-challenge defense content folded in
    assert "image.pas" in p


def test_generic_defense_phase_gated_toggle():
    """generic_defense_prompt includes the WRONG_PHASE block only when gated."""
    from agents.prompts import generic_defense_prompt
    cfg = AgentConfig(cli="c", model="m", api_key="k", base_url="u",
                      team_id="team1", own_prod_host="team1_prod")
    gated = generic_defense_prompt(cfg, service_name="svc", port=1, source_dir="/s",
                                   phase_gated=True)
    ungated = generic_defense_prompt(cfg, service_name="svc", port=1, source_dir="/s",
                                     phase_gated=False)
    assert "DO NOT SUBMIT" in gated
    assert "DO NOT SUBMIT" not in ungated
    assert "DEFENSE RECIPE" in ungated           # the actual recipe is kept


# ── status() / get_game_state fields ────────────────────────────────────


def test_status_async_fields():
    game = _make_game(mode="async", round_secs=30, total_rounds=2)
    st = game.status()
    assert st["mode"] == "async"
    assert st["round_secs"] == 30
    assert st["phase"] == Phase.INIT.value
    # seconds_to_rotation only once a tick has started
    assert "seconds_to_rotation" not in st


def test_status_sync_carries_mode_only():
    game = _make_game(mode="sync")
    st = game.status()
    assert st["mode"] == "sync"
    assert "round_secs" not in st
    assert "seconds_to_rotation" not in st


# ── LIVE-phase gate: submit accepted (not WRONG_PHASE) ───────────────────


class _PhaseStub:
    def __init__(self, phase: Phase) -> None:
        self.phase = phase
        self.round = 1

    def stop(self) -> None: ...
    def end_phase_early(self) -> None: ...


def test_submit_not_wrong_phase_at_live():
    game = _make_game()
    game.round_loop = _PhaseStub(Phase.LIVE)
    res = game.flag_server.submit("team1", "FLAG{NOSUCHFLAG0000000000000000000000}")
    assert res.reason != "WRONG_PHASE"   # LIVE accepts submissions

    game.round_loop = _PhaseStub(Phase.DEFENSE)
    res = game.flag_server.submit("team1", "FLAG{NOSUCHFLAG0000000000000000000000}")
    assert res.reason == "WRONG_PHASE"   # DEFENSE still gates (sync behavior)


# ── stop() in async mode kills agents before draining ───────────────────


def test_async_stop_kills_agents():
    game = _make_game()
    killed = []

    async def kill_all():
        killed.append(True)

    game.register_kill_all_agents(kill_all)
    game.round_loop = _PhaseStub(Phase.LIVE)

    asyncio.run(game.stop())
    assert killed == [True]
    assert game.is_game_over()


# ── agent supervisor crash-loop guard ───────────────────────────────────


class _Result:
    def __init__(self, exit_code: int = 1, timed_out: bool = False) -> None:
        self.stdout = b""
        self.stderr = b""
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.pkilled_at = None


class _FastExitMgr:
    """run_phase returns immediately (fast exit) every time."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_phase(self, backend, handle, prompt, *, timeout, model=None):
        self.calls += 1
        return _Result(exit_code=1)


def test_supervisor_gives_up_after_fast_exits(monkeypatch):
    """A crash-looping agent must back off and give up, not hot-loop forever."""
    # No real backoff sleeps in the test.
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    game = _make_game(agents=_agents("team1"))
    mgr = _FastExitMgr()
    cfg = AgentConfig(
        cli="claude --print", model="m", api_key="k", base_url="u",
        system_prompt="briefing",
    )

    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=cfg))

    assert mgr.calls == main.ASYNC_MAX_FAST_EXITS
    failed = [e for e in game.timeline.history()
              if e.type == EventType.AGENT_FAILED.value]
    assert any("gave up" in (e.payload or {}).get("error", "") for e in failed)


def test_drain_rekills_stragglers_before_cancel():
    """A task still blocked after the grace must trigger a re-kill (which
    unblocks it) rather than being cancelled (which would orphan the
    in-container process)."""
    game = _make_game()
    game._STOP_DRAIN_GRACE_SECS = 0.05  # don't wait 30s in a test
    kill_calls: list[bool] = []

    async def run():
        released = asyncio.Event()

        async def stuck():
            await released.wait()   # blocks until the "kill" releases it

        async def kill_all():
            kill_calls.append(True)
            released.set()          # a real kill makes run_command return

        task = asyncio.create_task(stuck())
        game._track_kickoffs([task])
        game.register_kill_all_agents(kill_all)
        await game._drain_kickoffs()
        return task

    task = asyncio.run(run())
    assert kill_calls == [True]
    assert task.done() and not task.cancelled()   # settled via kill, not cancel


def test_drain_cancels_when_no_kill_hook():
    """Sync mode (no kill hook): a wedged task is still cancelled as the
    last-resort backstop — behavior unchanged."""
    game = _make_game()
    game._STOP_DRAIN_GRACE_SECS = 0.05

    async def run():
        async def stuck():
            await asyncio.Event().wait()   # never released

        task = asyncio.create_task(stuck())
        game._track_kickoffs([task])
        await game._drain_kickoffs()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task

    task = asyncio.run(run())
    assert task.cancelled()


def test_supervisor_exits_when_game_over():
    """If the game is already over, the supervisor runs zero chunks."""
    game = _make_game(agents=_agents("team1"))
    game._game_over.set()
    mgr = _FastExitMgr()
    cfg = AgentConfig(cli="c", model="m", api_key="k", base_url="u",
                      system_prompt="briefing")

    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=cfg))
    assert mgr.calls == 0
