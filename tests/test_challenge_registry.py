"""challenges.registry — name -> Challenge factory, lifecycle hooks,
and per-challenge image / health-interval lookups.

These tests pin the registry's public surface so adding a new challenge
(append two-to-four entries to the registry, run the suite) breaks
loudly if the contract slips.
"""

from __future__ import annotations

import pytest

from challenges.interface import Challenge, VulboxTarget
from challenges.nautilus_ico import IcoChallenge
from challenges.notes import NotesChallenge
from challenges import registry


# ── build_challenge / known_challenge_names ───────────────────────────


def test_build_challenge_returns_notes_instance():
    ch = registry.build_challenge("notes")
    assert isinstance(ch, NotesChallenge)
    assert ch.name == "notes"


def test_build_challenge_returns_ico_instance():
    ch = registry.build_challenge("nautilus-ico")
    assert isinstance(ch, IcoChallenge)
    assert ch.name == "nautilus-ico"


def test_build_challenge_unknown_name_raises_with_helpful_message():
    with pytest.raises(KeyError, match="unknown challenge: 'bogus'"):
        registry.build_challenge("bogus")
    # The error lists known names so operators can spot typos.
    with pytest.raises(KeyError, match="known:"):
        registry.build_challenge("bogus")


def test_known_challenge_names_is_sorted_and_includes_both():
    names = registry.known_challenge_names()
    assert names == sorted(names)
    assert "notes" in names
    assert "nautilus-ico" in names


def test_factories_return_fresh_instances():
    """Zero-arg factories ensure no state leaks across calls (e.g. the
    @cached_property values on Challenge are per-instance)."""
    a = registry.build_challenge("notes")
    b = registry.build_challenge("notes")
    assert a is not b


# ── initial_start dispatch ────────────────────────────────────────────


def _target(host: str = "team1_prod", *, port: int = 5000) -> VulboxTarget:
    return VulboxTarget(host=host, ports={"service": port}, meta={})


def _record_exec():
    """Stub exec_in_container that records every (host, cmd) call and
    returns (0, '') unless a per-call override is queued."""
    calls: list[tuple[str, str]] = []
    queue: list[tuple[int, str]] = []

    def fake(host: str, cmd: str) -> tuple[int, str]:
        calls.append((host, cmd))
        if queue:
            return queue.pop(0)
        return (0, "")

    return fake, calls, queue


def test_initial_start_notes_ships_script_and_spawns():
    """The notes hook issues two exec calls: deliver the daemon source,
    then nohup-spawn it. Both go through the recorded exec_in."""
    exec_in, calls, _ = _record_exec()
    ch = registry.build_challenge("notes")
    registry.initial_start(ch, _target(port=5000), exec_in)
    assert len(calls) == 2
    deliver_cmd = calls[0][1]
    assert "base64 -d" in deliver_cmd
    assert "server.py" in deliver_cmd
    spawn_cmd = calls[1][1]
    assert "nohup python3" in spawn_cmd
    assert "5000" in spawn_cmd


def test_initial_start_notes_raises_when_delivery_fails():
    """If the script-delivery exec returns non-zero, the hook raises so
    GameMgr.deploy_challenges surfaces the failure immediately."""
    exec_in, _calls, queue = _record_exec()
    queue.append((1, "disk full"))  # script-delivery exec fails
    ch = registry.build_challenge("notes")
    with pytest.raises(RuntimeError, match="script delivery failed"):
        registry.initial_start(ch, _target(), exec_in)


def test_initial_start_ico_invokes_restart_sh():
    """The ico hook is a single docker exec of /arena/restart.sh."""
    exec_in, calls, _ = _record_exec()
    ch = registry.build_challenge("nautilus-ico")
    registry.initial_start(ch, _target(host="team2_prod", port=4265), exec_in)
    assert calls == [("team2_prod", "/arena/restart.sh")]


def test_initial_start_ico_tolerates_restart_failure():
    """If /arena/restart.sh fails (e.g. fpc not yet ready), the hook
    logs a warning but doesn't raise — the functionality_test probe is
    the real readiness signal."""
    exec_in, _calls, queue = _record_exec()
    queue.append((1, "fpc: not found"))
    ch = registry.build_challenge("nautilus-ico")
    # No raise, no return value.
    registry.initial_start(ch, _target(host="team1_prod", port=4265), exec_in)


def test_initial_start_unregistered_challenge_is_noop():
    """A Challenge whose name isn't in _INITIAL_START performs no exec
    calls — image-baked entrypoints handle their own startup."""
    class _SilentChallenge(Challenge):
        @property
        def name(self) -> str:
            return "silent"

        @property
        def vulbox(self):
            from challenges.interface import VulboxImage
            return VulboxImage(reference="x", exposed_ports={"service": 1})

        @property
        def functionality_test(self):  # pragma: no cover — unused
            raise NotImplementedError

        @property
        def flag_handler(self):  # pragma: no cover — unused
            raise NotImplementedError

        @property
        def docs(self):
            from challenges.interface import ChallengeDocs
            return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")

    exec_in, calls, _ = _record_exec()
    registry.initial_start(_SilentChallenge(), _target(), exec_in)
    assert calls == []


# ── health_interval_secs ──────────────────────────────────────────────


def test_health_interval_secs_returns_registered_value():
    """ico is the only challenge in tree with a per-name override."""
    assert registry.health_interval_secs("nautilus-ico", 10.0) == 30.0


def test_health_interval_secs_falls_back_to_default():
    """notes has no override; the caller's default is returned untouched."""
    assert registry.health_interval_secs("notes", 17.5) == 17.5


def test_health_interval_secs_default_for_unknown_name():
    assert registry.health_interval_secs("bogus", 42.0) == 42.0


# ── build_images dispatch ─────────────────────────────────────────────


class _Cfg:
    """Minimal stand-in for GameConfig.challenges — only `.name` is read."""
    def __init__(self, *names: str) -> None:
        self.challenges = [type("_C", (), {"name": n})() for n in names]


def test_build_images_invokes_per_challenge_builder(monkeypatch):
    """Each `cfg.challenges` entry with an entry in `_IMAGE_BUILDERS`
    triggers exactly one builder call; the returned tag goes into the
    result list."""
    calls: list[str] = []

    def fake_ico_build() -> str:
        calls.append("ico")
        return "cyberarena/chal-nautilus-ico:test"

    monkeypatch.setitem(registry._IMAGE_BUILDERS, "nautilus-ico", fake_ico_build)
    tags = registry.build_images(_Cfg("notes", "nautilus-ico"))
    # notes has no builder -> skipped; ico builds exactly once.
    assert calls == ["ico"]
    assert tags == ["cyberarena/chal-nautilus-ico:test"]


def test_build_images_skips_challenges_without_a_builder():
    """notes-only configs produce no builds (notes uses prod-base from
    topology, not a per-challenge image)."""
    assert registry.build_images(_Cfg("notes")) == []


def test_build_images_empty_when_no_challenges_configured():
    assert registry.build_images(_Cfg()) == []
