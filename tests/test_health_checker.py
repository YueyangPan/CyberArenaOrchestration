"""HealthChecker unit tests.

Verify the 4-level chain emits one SERVICE_HEALTH_CHECK per level in
the right order, against a stubbed docker-exec and no Service adapter.
"""

from __future__ import annotations

import asyncio

import pytest

from challenges.source import ChallengeSpec, FlagMechanism, HealthLevel
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from health.checker import HealthChecker


def _spec(**overrides) -> ChallengeSpec:
    md = {
        "restart_cmd": "echo",
        "flag_path": "/flag",
        "health_check": {
            "process_pattern": "ico",
            "checker_command": "/arena/checker.sh",
        },
    }
    md.update(overrides.pop("metadata", {}))
    return ChallengeSpec(
        name=overrides.get("name", "nautilus-ico"),
        source="nautilus",
        port=overrides.get("port", 4265),
        image_tag="x",
        flag_mechanism=FlagMechanism.FILE_AT_PATH,
        metadata=md,
    )


def _checker(timeline: Timeline, exec_results: dict[str, tuple[int, str]]):
    """Build a checker whose exec returns (rc, output) per command pattern.
    Keys are matched by substring; first hit wins."""
    def fake_exec(container, cmd):
        for pat, result in exec_results.items():
            if pat in cmd:
                return result
        return (0, "ok\n")

    return HealthChecker(
        timeline,
        exec_in_container=fake_exec,
        service_resolver=lambda team, svc: None,
        handle_resolver=lambda name: None,  # not used when service_resolver returns None
        status_resolver=lambda: {"round": 1, "phase": "ATTACK"},
    )


def _checks(timeline: Timeline) -> list:
    return [e for e in timeline.history() if e.type == EventType.SERVICE_HEALTH_CHECK.value]


def test_chain_emits_four_events_in_order_all_passing():
    tl = Timeline()
    checker = _checker(tl, {})  # everything returns (0, "ok")

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="team1_prod", spec=_spec(),
        )

    asyncio.run(go())
    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == [
        HealthLevel.PROCESS.value,
        HealthLevel.TCP.value,
        HealthLevel.PROTOCOL.value,
        HealthLevel.CHECKER.value,
    ]
    assert all(e.payload["passed"] for e in _checks(tl))


def test_process_fails_when_pgrep_returns_nonzero():
    tl = Timeline()
    checker = _checker(tl, {"pgrep": (1, "")})

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=_spec(),
        )

    asyncio.run(go())
    by_level = {e.payload["level"]: e.payload["passed"] for e in _checks(tl)}
    assert by_level[HealthLevel.PROCESS.value] is False
    assert by_level[HealthLevel.TCP.value] is True  # other levels still run


def test_tcp_fails_when_socket_connect_nonzero():
    tl = Timeline()
    checker = _checker(tl, {"socket": (1, "")})

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=_spec(),
        )

    asyncio.run(go())
    by_level = {e.payload["level"]: e.payload["passed"] for e in _checks(tl)}
    assert by_level[HealthLevel.TCP.value] is False


def test_checker_fails_when_checker_cmd_nonzero():
    tl = Timeline()
    checker = _checker(tl, {"checker.sh": (1, "FAIL: vuln gone\n")})

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=_spec(),
        )

    asyncio.run(go())
    by_level = {e.payload["level"]: (e.payload["passed"], e.payload["detail"]) for e in _checks(tl)}
    passed, detail = by_level[HealthLevel.CHECKER.value]
    assert passed is False
    assert "FAIL" in detail


def test_no_process_pattern_skipped_as_passing():
    """spec with no process_pattern → PROCESS passes with "skipped" detail."""
    tl = Timeline()
    checker = _checker(tl, {})
    spec = _spec(metadata={"health_check": {"checker_command": "/arena/checker.sh"}})

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=spec,
        )

    asyncio.run(go())
    process = next(e for e in _checks(tl) if e.payload["level"] == HealthLevel.PROCESS.value)
    assert process.payload["passed"] is True
    assert "skipped" in process.payload["detail"]


def test_no_checker_cmd_aliases_to_protocol():
    """spec with no checker_command → CHECKER reuses PROTOCOL outcome."""
    tl = Timeline()
    # PROTOCOL has no service adapter → passes with reason; CHECKER inherits.
    checker = _checker(tl, {})
    spec = _spec(metadata={"health_check": {"process_pattern": "ico"}})

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=spec,
        )

    asyncio.run(go())
    chk = next(e for e in _checks(tl) if e.payload["level"] == HealthLevel.CHECKER.value)
    assert chk.payload["passed"] is True
    assert "aliased to PROTOCOL" in chk.payload["detail"]


def test_status_resolver_stamps_round_and_phase():
    tl = Timeline()
    checker = HealthChecker(
        tl,
        exec_in_container=lambda c, cmd: (0, ""),
        service_resolver=lambda t, s: None,
        handle_resolver=lambda n: None,
        status_resolver=lambda: {"round": 7, "phase": "ATTACK"},
    )

    async def go():
        await checker.run_chain(
            team_id="team1", service="nautilus-ico",
            container_name="c", spec=_spec(),
        )

    asyncio.run(go())
    for e in _checks(tl):
        assert e.round_id == 7
        assert e.phase == "ATTACK"
