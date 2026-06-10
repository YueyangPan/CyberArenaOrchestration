"""FlagServer unit tests.

FlagServer owns plant + rotation + submission verification. Wired
into GameMgr but tested here in isolation against stubbed deps so we
don't need docker.

Stage 3 (challenge interface v2): the per-mechanism branching is gone.
FlagServer talks to `challenge.flag_handler` directly; the stub
challenge below records plant calls so we can pin the contract.
"""

from __future__ import annotations

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    FlagHandler,
    FunctionalityTest,
    VulboxImage,
    VulboxTarget,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from flag import FlagServer
from gamemgr.events import EventType
from gamemgr.timeline import Timeline


def _ctx(timeline: Timeline, *, round: int = 7, phase: str = "ATTACK") -> FacilityContext:
    """Local in-process FacilityContext for FlagServer tests. No exec
    port — FlagServer drives exec through the target's callable, which
    `_target_factory` stubs."""
    return FacilityContext(
        status=LocalStatusPort(lambda: {"round": round, "phase": phase}),
        events=LocalEventSink(timeline),
    )


class _StubFlagHandler(FlagHandler):
    """FlagHandler stand-in; records every plant + returns a deterministic
    handle so tests can assert what got dispatched."""

    def __init__(self) -> None:
        self.plants: list[tuple[VulboxTarget, str]] = []

    @property
    def name(self) -> str:
        return "stub-flag-handler"

    def plant(self, target: VulboxTarget, flag: str) -> str:
        self.plants.append((target, flag))
        return f"h-{len(self.plants)}"

    def retrieve(self, target: VulboxTarget, handle: str) -> str | None:
        return None


class _StubFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str:
        return "stub-functionality"

    def run(self, target):  # pragma: no cover — unused in these tests
        raise NotImplementedError


class _StubChallenge(Challenge):
    """Minimal Challenge for FlagServer tests. Each instance carries its
    own _StubFlagHandler so plant calls are inspectable per challenge."""

    def __init__(self, name: str = "notes", port: int = 5000) -> None:
        self._name = name
        self._port = port
        self._flag_handler = _StubFlagHandler()
        self._functionality_test = _StubFunctionalityTest()
        self._vulbox = VulboxImage(
            reference=f"stub/{name}", exposed_ports={"service": port},
        )
        self._docs = ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")

    @property
    def name(self) -> str:
        return self._name

    @property
    def vulbox(self) -> VulboxImage:
        return self._vulbox

    @property
    def functionality_test(self) -> FunctionalityTest:
        return self._functionality_test

    @property
    def flag_handler(self) -> FlagHandler:
        return self._flag_handler

    @property
    def docs(self) -> ChallengeDocs:
        return self._docs


def _target_factory(team_id: str, ch: Challenge) -> VulboxTarget:
    """Mimics GameMgr.target_for without the topology indirection."""
    return VulboxTarget(
        host=f"{team_id}_prod",
        ports=dict(ch.vulbox.exposed_ports),
        meta={"team_id": team_id, "exec_in_container": lambda *_: (0, "")},
    )


def _server(challenge_by_owner: dict | None = None, *, phase: str = "ATTACK"):
    timeline = Timeline()
    server = FlagServer(
        _ctx(timeline, phase=phase),
        challenge_by_owner=challenge_by_owner if challenge_by_owner is not None else {},
        target_factory=_target_factory,
    )
    return server, timeline


# ── planting ──────────────────────────────────────────────────────────


def test_plant_dispatches_to_flag_handler_and_updates_store():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})

    planted = server.plant_flags(round_id=1)
    assert len(planted) == 2
    assert {t[0] for t in planted} == {"team1", "team2"}
    # FlagHandler.plant was called once per team with the right target.
    assert {p[0].host for p in ch._flag_handler.plants} == {"team1_prod", "team2_prod"}
    # Store populated by (team, service) -> flag.
    assert set(server.store.planted.keys()) == {("team1", "notes"), ("team2", "notes")}


def test_plant_stashes_handle_for_future_retrieve():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch})
    server.plant_flags(round_id=1)
    # The stub handler returns "h-1" for the first plant.
    assert server._handles[("team1", "notes")] == "h-1"


def test_replant_replaces_prior_flag_in_store():
    """Rotation invariant: re-planting drops the old flag from the
    inverted index so it submits as UNKNOWN."""
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch})

    p1 = server.plant_flags(round_id=1)
    old_flag = p1[0][2]
    p2 = server.plant_flags(round_id=2)
    new_flag = p2[0][2]
    assert old_flag != new_flag
    assert old_flag not in server.store.by_flag
    assert new_flag in server.store.by_flag


# ── rotate (timeline records + idempotency) ──────────────────────────


def test_rotate_records_flag_planted_per_team():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})

    server.rotate(round_id=1)
    planted_events = [e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value]
    assert len(planted_events) == 2
    assert {e.team_id for e in planted_events} == {"team1", "team2"}
    for e in planted_events:
        assert e.round_id == 1
        assert e.payload["service"] == "notes"
        # Flag strings never appear on the timeline.
        assert "FLAG{" not in str(e.payload)


def test_rotate_is_idempotent_per_round():
    """A second rotate(round_id=N) must NOT re-plant — flag rotation
    happens once per DEFENSE entry; a re-entry should not invalidate
    the just-planted flag."""
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch})

    server.rotate(round_id=1)
    first_flag = server.store.planted[("team1", "notes")]
    server.rotate(round_id=1)  # duplicate
    assert server.store.planted[("team1", "notes")] == first_flag
    planted_for_round = [
        e for e in tl.history()
        if e.type == EventType.FLAG_PLANTED.value and e.round_id == 1
    ]
    assert len(planted_for_round) == 1


# ── submission ───────────────────────────────────────────────────────


def test_submit_accepted_records_event_without_leaking_flag():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})
    server.plant_flags(round_id=1)
    target_flag = server.store.planted[("team1", "notes")]

    result = server.submit("team2", target_flag)
    assert result.accepted is True
    assert result.reason == "ACCEPTED"
    assert result.victim_team_id == "team1"

    submitted = [e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value]
    assert len(submitted) == 1
    ev = submitted[0]
    # round + phase stamped from status_resolver (test stub returns 7 / "ATTACK")
    assert ev.round_id == 7
    assert ev.phase == "ATTACK"
    assert ev.payload["accepted"] is True
    assert ev.payload["victim_team_id"] == "team1"
    assert target_flag not in str(ev.payload)


def test_submit_unknown_records_event_without_victim_or_service():
    server, tl = _server()
    result = server.submit("team1", "FLAG{NEVER_PLANTED}")
    assert result.accepted is False and result.reason == "UNKNOWN"
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.payload["reason"] == "UNKNOWN"
    assert ev.payload["victim_team_id"] is None


# ── phase-gating ─────────────────────────────────────────────────────


def test_submit_during_defense_rejected_as_wrong_phase():
    """Submissions during DEFENSE must be rejected without consulting the
    FlagStore — a confused defense agent reusing an ATTACK-phase flag
    string should NOT burn its DUPLICATE slot for the next ATTACK."""
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch}, phase="DEFENSE")
    server.plant_flags(round_id=1)
    target_flag = server.store.planted[("team1", "notes")]

    result = server.submit("team2", target_flag)
    assert result.accepted is False
    assert result.reason == "WRONG_PHASE"
    # detail carries a human-readable explanation for the agent.
    assert "defense" in result.detail.lower()
    # The store wasn't touched — the flag is still NOT in team2's
    # submitted set, so a real ATTACK-phase resubmit would still succeed.
    assert target_flag not in server.store.submissions.get("team2", set())

    # FLAG_SUBMITTED still lands on the timeline (panel needs visibility)
    # but with reason=WRONG_PHASE.
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.payload["reason"] == "WRONG_PHASE"


def test_submit_during_defense_then_attack_resubmit_accepted():
    """After WRONG_PHASE rejection during DEFENSE, the same flag in the
    next ATTACK phase is still ACCEPTED — store wasn't tainted."""
    ch = _StubChallenge()
    timeline = Timeline()
    current = {"phase": "DEFENSE"}
    ctx = FacilityContext(
        status=LocalStatusPort(lambda: {"round": 7, "phase": current["phase"]}),
        events=LocalEventSink(timeline),
    )
    server = FlagServer(
        ctx,
        challenge_by_owner={("team1", "notes"): ch, ("team2", "notes"): ch},
        target_factory=_target_factory,
    )
    server.plant_flags(round_id=1)
    target_flag = server.store.planted[("team1", "notes")]

    # DEFENSE submit -> WRONG_PHASE
    r1 = server.submit("team2", target_flag)
    assert r1.reason == "WRONG_PHASE"

    # Phase flips to ATTACK; same flag is still acceptable.
    current["phase"] = "ATTACK"
    r2 = server.submit("team2", target_flag)
    assert r2.accepted is True
    assert r2.reason == "ACCEPTED"


# ── setup snapshot ───────────────────────────────────────────────────


def test_emit_planted_setup_records_one_per_planted_entry():
    """Used by GameMgr.emit_setup_events for the pre-loop snapshot."""
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})
    server.plant_initial_flags()
    before = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    server.emit_planted_setup()
    after = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    assert after - before == 2
    for e in tl.history():
        if e.type == EventType.FLAG_PLANTED.value:
            assert e.round_id == 0
