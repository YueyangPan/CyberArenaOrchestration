"""FlagServer unit tests.

FlagServer owns plant + rotation + submission verification. Wired
into GameMgr but tested here in isolation against stubbed deps so we
don't need docker.
"""

from __future__ import annotations

from challenges.source import ChallengeSpec, FlagMechanism
from flag import FlagServer
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from models.topology import Team, TopologyGraph


class _StubService:
    """Service adapter stand-in for SERVICE_PROTOCOL plants."""
    def __init__(self, name="notes", port=5000):
        self.name = name
        self.port = port
        self.plant_calls: list = []

    def plant_flag(self, handle, flag):
        self.plant_calls.append((handle, flag))


def _topology(*team_ids: str):
    teams = [
        Team(id=tid, dev=f"{tid}_dev", prod=f"{tid}_prod", token=f"tok-{tid}")
        for tid in team_ids
    ]
    return TopologyGraph(teams=teams, nodes=[], networks=[])


def _server(*, service_by_owner=None, spec_by_owner=None, exec_calls=None):
    timeline = Timeline()
    exec_log: list[tuple[str, str]] = [] if exec_calls is None else exec_calls

    def fake_exec(container, cmd):
        exec_log.append((container, cmd))
        return (0, "")

    server = FlagServer(
        timeline,
        topology=_topology("team1", "team2"),
        service_by_owner=service_by_owner if service_by_owner is not None else {},
        spec_by_owner=spec_by_owner if spec_by_owner is not None else {},
        handle_resolver=lambda name: name,  # opaque; only SERVICE_PROTOCOL plant uses
        exec_in_container=fake_exec,
        status_resolver=lambda: {"round": 7, "phase": "ATTACK"},
    )
    return server, timeline


def _file_at_path_spec(name="nautilus-ico") -> ChallengeSpec:
    return ChallengeSpec(
        name=name, source="nautilus", port=4265,
        image_tag="x", flag_mechanism=FlagMechanism.FILE_AT_PATH,
        metadata={"flag_path": "/flag", "restart_cmd": "echo"},
    )


# ── planting ──────────────────────────────────────────────────────────


def test_plant_service_protocol_calls_adapter_and_updates_store():
    svc = _StubService()
    server, _tl = _server(service_by_owner={("team1", "notes"): svc, ("team2", "notes"): svc})

    planted = server.plant_flags(round_id=1)
    assert sorted(planted) == sorted(planted)  # tuple sanity
    assert len(planted) == 2
    assert {t[0] for t in planted} == {"team1", "team2"}
    # Adapter received plant for both teams.
    assert {h for h, _ in svc.plant_calls} == {"team1_prod", "team2_prod"}
    # Store populated by (team, service) → flag.
    assert set(server.store.planted.keys()) == {("team1", "notes"), ("team2", "notes")}


def test_plant_file_at_path_runs_docker_exec_write(tmp_path):
    exec_log: list = []
    server, _tl = _server(
        spec_by_owner={("team1", "nautilus-ico"): _file_at_path_spec()},
        exec_calls=exec_log,
    )
    planted = server.plant_flags(round_id=1)
    assert len(planted) == 1
    container, cmd = exec_log[0]
    assert container == "team1_prod"
    assert "/flag" in cmd
    assert "chmod 644" in cmd


def test_replant_replaces_prior_flag_in_store():
    """Rotation invariant: re-planting drops the old flag from the
    inverted index so it submits as UNKNOWN."""
    svc = _StubService()
    server, _tl = _server(service_by_owner={("team1", "notes"): svc})

    p1 = server.plant_flags(round_id=1)
    old_flag = p1[0][2]
    p2 = server.plant_flags(round_id=2)
    new_flag = p2[0][2]
    assert old_flag != new_flag
    # Old flag no longer in by_flag → submitting it would return UNKNOWN.
    assert old_flag not in server.store.by_flag
    assert new_flag in server.store.by_flag


# ── rotate (timeline records + idempotency) ──────────────────────────


def test_rotate_records_flag_planted_per_team():
    svc = _StubService()
    server, tl = _server(service_by_owner={
        ("team1", "notes"): svc, ("team2", "notes"): svc,
    })

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
    svc = _StubService()
    server, tl = _server(service_by_owner={("team1", "notes"): svc})

    server.rotate(round_id=1)
    first_flag = server.store.planted[("team1", "notes")]
    server.rotate(round_id=1)  # duplicate
    assert server.store.planted[("team1", "notes")] == first_flag
    # Only one FLAG_PLANTED recorded for round 1.
    planted_for_round = [
        e for e in tl.history()
        if e.type == EventType.FLAG_PLANTED.value and e.round_id == 1
    ]
    assert len(planted_for_round) == 1


# ── submission ───────────────────────────────────────────────────────


def test_submit_accepted_records_event_without_leaking_flag():
    svc = _StubService()
    server, tl = _server(service_by_owner={
        ("team1", "notes"): svc, ("team2", "notes"): svc,
    })
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
    # Payload mirrors SubmitResult but never contains the flag string itself.
    assert ev.payload["accepted"] is True
    assert ev.payload["victim_team_id"] == "team1"
    assert target_flag not in str(ev.payload)


def test_submit_unknown_records_event_without_victim_or_service():
    server, tl = _server()  # store empty
    result = server.submit("team1", "FLAG{NEVER_PLANTED}")
    assert result.accepted is False and result.reason == "UNKNOWN"
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.payload["reason"] == "UNKNOWN"
    assert ev.payload["victim_team_id"] is None


# ── setup snapshot ───────────────────────────────────────────────────


def test_emit_planted_setup_records_one_per_planted_entry():
    """Used by GameMgr.emit_setup_events for the pre-loop snapshot."""
    svc = _StubService()
    server, tl = _server(service_by_owner={
        ("team1", "notes"): svc, ("team2", "notes"): svc,
    })
    server.plant_initial_flags()
    # plant_initial_flags doesn't record (rotate does); emit_planted_setup
    # is the explicit pre-loop snapshot.
    before = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    server.emit_planted_setup()
    after = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    assert after - before == 2
    for e in tl.history():
        if e.type == EventType.FLAG_PLANTED.value:
            assert e.round_id == 0
