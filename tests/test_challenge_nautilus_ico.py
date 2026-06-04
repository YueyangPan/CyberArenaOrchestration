"""IcoChallenge unit tests — exercise the v2 abstract shape against a
stubbed `target.meta["exec_in_container"]` callable. No docker needed.

Wiring into the orchestrator (FlagServer / HealthChecker / restart_service)
lands in Stages 3-5; this test pins the per-challenge contract in isolation.
"""

from __future__ import annotations

import pytest

from challenges.interface import VulboxTarget
from challenges.nautilus_ico import IcoChallenge


def _stub_exec(replies):
    """Build an exec_in_container that returns canned (rc, output) pairs
    in order. Each entry is matched in command order. Unknown calls return
    (rc=0, "")."""
    calls: list[tuple[str, str]] = []

    def fake(container_name: str, cmd: str) -> tuple[int, str]:
        calls.append((container_name, cmd))
        if replies:
            return replies.pop(0)
        return (0, "")

    return fake, calls


def _target(exec_fn):
    return VulboxTarget(
        host="team1_prod",
        ports={"service": 4265},
        meta={
            "team_id": "team1",
            "exec_in_container": exec_fn,
        },
    )


def test_ico_challenge_composition():
    ch = IcoChallenge()
    assert ch.name == "nautilus-ico"
    assert ch.vulbox.exposed_ports == {"service": 4265}
    assert ch.vulbox.reference.startswith("cyberarena/chal-nautilus-ico:")
    assert ch.functionality_test.required_ports == ("service",)
    # FlagHandler uses docker exec; doesn't need a named port.
    assert ch.flag_handler.required_ports == ()
    # Defense-enabled: restart_handler is set (unlike notes).
    assert ch.restart_handler is not None
    assert ch.restart_handler.required_ports == ("service",)


def test_functionality_test_all_pass():
    """PROCESS pgrep OK, TCP probe OK, /arena/checker.sh OK -> all 4 pass."""
    exec_fn, _ = _stub_exec([
        (0, ""),                # PROCESS pgrep
        (0, ""),                # TCP socket-connect probe
        (0, "checker OK\n"),    # CHECKER /arena/checker.sh
    ])
    ch = IcoChallenge()
    result = ch.functionality_test.run(_target(exec_fn))
    assert result.name == "nautilus-ico"
    assert result.passed is True
    levels = {c.name: c for c in result.children}
    assert set(levels) == {"process", "tcp", "protocol", "checker"}
    assert all(c.passed for c in result.children)
    # PROTOCOL aliases CHECKER by design.
    assert "aliased" in levels["protocol"].detail.lower()


def test_functionality_test_checker_failure_cascades_to_protocol():
    """If /arena/checker.sh exits non-zero, CHECKER fails and PROTOCOL
    inherits (alias relationship)."""
    exec_fn, _ = _stub_exec([
        (0, ""),                # PROCESS OK
        (0, ""),                # TCP OK
        (1, "checker FAIL\n"),  # CHECKER non-zero
    ])
    ch = IcoChallenge()
    result = ch.functionality_test.run(_target(exec_fn))
    levels = {c.name: c for c in result.children}
    assert levels["checker"].passed is False
    assert levels["protocol"].passed is False
    assert result.passed is False


def test_flag_handler_plant_writes_flag_path():
    """plant returns the FLAG_PATH ('/flag') as the opaque handle."""
    exec_fn, calls = _stub_exec([(0, "")])
    ch = IcoChallenge()
    handle = ch.flag_handler.plant(_target(exec_fn), "FLAG{ICO_TEST}")
    assert handle == "/flag"
    # One docker exec issued, and the flag is baked into the command.
    assert len(calls) == 1
    cname, cmd = calls[0]
    assert cname == "team1_prod"
    assert "/flag" in cmd
    assert "FLAG{ICO_TEST}" in cmd


def test_flag_handler_plant_raises_on_failure():
    exec_fn, _ = _stub_exec([(1, "permission denied")])
    ch = IcoChallenge()
    with pytest.raises(RuntimeError, match="plant_flag failed"):
        ch.flag_handler.plant(_target(exec_fn), "FLAG{X}")


def test_flag_handler_retrieve_round_trip():
    exec_fn, _ = _stub_exec([(0, "FLAG{ROUNDTRIP}\n")])
    ch = IcoChallenge()
    flag = ch.flag_handler.retrieve(_target(exec_fn), "/flag")
    assert flag == "FLAG{ROUNDTRIP}"


def test_flag_handler_retrieve_returns_none_on_missing_file():
    exec_fn, _ = _stub_exec([(1, "")])
    ch = IcoChallenge()
    assert ch.flag_handler.retrieve(_target(exec_fn), "/flag") is None


def test_restart_handler_all_steps_pass():
    """/arena/restart.sh OK + port 4265 reachable -> 3 leaves pass."""
    exec_fn, _ = _stub_exec([
        (0, "ico restarted, pid=42\n"),   # /arena/restart.sh
        (0, ""),                          # smoke TCP probe
    ])
    ch = IcoChallenge()
    result = ch.restart_handler.run(_target(exec_fn))
    assert result.passed is True
    leaves = {c.name: c for c in result.children}
    assert set(leaves) == {"compile", "spawn", "smoke"}
    assert all(c.passed for c in result.children)


def test_restart_handler_script_failure_skips_smoke():
    """If restart.sh fails, smoke is reported as skipped/failed without
    probing the (stale) port."""
    exec_fn, calls = _stub_exec([
        (1, "fpc: compile error\n"),
    ])
    ch = IcoChallenge()
    result = ch.restart_handler.run(_target(exec_fn))
    leaves = {c.name: c for c in result.children}
    assert leaves["compile"].passed is False
    assert leaves["spawn"].passed is False
    assert leaves["smoke"].passed is False
    assert "skipped" in leaves["smoke"].detail
    assert result.passed is False
    # Only one exec call was made — smoke probe wasn't issued.
    assert len(calls) == 1


def test_restart_handler_smoke_failure_when_bind_failed():
    """restart.sh claimed success but port didn't bind -> smoke fails,
    compile/spawn pass (the script said so)."""
    exec_fn, _ = _stub_exec([
        (0, "ico restarted, pid=99\n"),   # script claims success
        (1, ""),                          # but TCP connect fails
    ])
    ch = IcoChallenge()
    result = ch.restart_handler.run(_target(exec_fn))
    leaves = {c.name: c for c in result.children}
    assert leaves["compile"].passed is True
    assert leaves["spawn"].passed is True
    assert leaves["smoke"].passed is False
    assert result.passed is False
