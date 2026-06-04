"""HealthChecker unit tests.

Stage 4 (challenge interface v2): HealthChecker is a thin wrapper that
delegates to `challenge.functionality_test.run(target)` and records one
SERVICE_HEALTH_CHECK per CheckResult leaf. Per-mechanism branching is
gone from the framework — these tests pin the wrapper contract.
"""

from __future__ import annotations

import asyncio

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    CheckResult,
    FlagHandler,
    FunctionalityTest,
    VulboxImage,
    VulboxTarget,
)
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from health.checker import HealthChecker


class _StubFT(FunctionalityTest):
    """Returns a pre-built CheckResult tree. Tests configure the tree
    per scenario."""

    def __init__(self, tree: CheckResult) -> None:
        self._tree = tree

    @property
    def name(self) -> str:
        return "stub-ft"

    def run(self, target: VulboxTarget) -> CheckResult:
        return self._tree


class _StubFH(FlagHandler):
    @property
    def name(self) -> str:
        return "stub-fh"

    def plant(self, target, flag):  # pragma: no cover — unused here
        raise NotImplementedError

    def retrieve(self, target, handle):
        return None


class _StubCh(Challenge):
    def __init__(self, tree: CheckResult) -> None:
        self._ft = _StubFT(tree)
        self._fh = _StubFH()

    @property
    def name(self) -> str:
        return "stub"

    @property
    def vulbox(self) -> VulboxImage:
        return VulboxImage(reference="stub", exposed_ports={"service": 1})

    @property
    def functionality_test(self) -> FunctionalityTest:
        return self._ft

    @property
    def flag_handler(self) -> FlagHandler:
        return self._fh

    @property
    def docs(self) -> ChallengeDocs:
        return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")


def _target() -> VulboxTarget:
    return VulboxTarget(host="team1_prod", ports={"service": 1}, meta={})


def _checks(tl: Timeline) -> list:
    return [e for e in tl.history() if e.type == EventType.SERVICE_HEALTH_CHECK.value]


def _run(checker, **kwargs):
    return asyncio.run(checker.run(**kwargs))


def test_run_emits_one_event_per_leaf_in_pre_order():
    """4-leaf tree (the standard process/tcp/protocol/checker shape)
    yields 4 events whose `level` payload matches the leaf names."""
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="process", passed=True, detail="pgrep rc=0"),
        CheckResult(name="tcp", passed=True, detail="ok"),
        CheckResult(name="protocol", passed=True, detail="ok"),
        CheckResult(name="checker", passed=True, detail="checker ok"),
    ])
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 1, "phase": "ATTACK"})
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))

    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == ["process", "tcp", "protocol", "checker"]
    assert all(e.payload["passed"] for e in _checks(tl))


def test_run_propagates_failure_per_leaf():
    tree = CheckResult(name="root", passed=False, children=[
        CheckResult(name="process", passed=True, detail=""),
        CheckResult(name="tcp", passed=False, detail="connection refused"),
    ])
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 1, "phase": "ATTACK"})
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))

    by_level = {e.payload["level"]: e.payload for e in _checks(tl)}
    assert by_level["process"]["passed"] is True
    assert by_level["tcp"]["passed"] is False
    assert "connection refused" in by_level["tcp"]["detail"]


def test_run_returns_root_check_result():
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="single", passed=True, detail=""),
    ])
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 1, "phase": "ATTACK"})
    result = _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    assert result is tree


def test_run_handles_single_leaf_tree():
    """A challenge whose FunctionalityTest returns a single leaf (no
    children) should still emit one event."""
    tree = CheckResult(name="alive", passed=True, detail="ok")
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 1, "phase": "ATTACK"})
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    checks = _checks(tl)
    assert len(checks) == 1
    assert checks[0].payload["level"] == "alive"


def test_run_walks_nested_trees():
    """A deeper tree emits one event per leaf, parent rollups omitted."""
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="net", passed=True, children=[
            CheckResult(name="tcp", passed=True),
            CheckResult(name="tls", passed=True),
        ]),
        CheckResult(name="checker", passed=True),
    ])
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 1, "phase": "ATTACK"})
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    levels = [e.payload["level"] for e in _checks(tl)]
    # No "net" or "root" — those are internal rollups.
    assert levels == ["tcp", "tls", "checker"]


def test_status_resolver_stamps_round_and_phase():
    tree = CheckResult(name="single", passed=True)
    tl = Timeline()
    checker = HealthChecker(tl, status_resolver=lambda: {"round": 7, "phase": "ATTACK"})
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    ev = _checks(tl)[0]
    assert ev.round_id == 7
    assert ev.phase == "ATTACK"
