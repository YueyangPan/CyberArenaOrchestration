"""DEFCON: ArtifactStore path allocator.

Pins the layout reserved for forensic / replay / scoring milestones so
those landings don't have to re-decide paths. v1 fills 3 kinds
(api-logs, agent-logs, service-logs); the other 5 are reserved as
empty subdirs.

No docker needed — pure pathlib.
"""

from __future__ import annotations

import pytest

from gamemgr.artifacts import ArtifactKind, ArtifactStore


# ── kinds / enum invariants ────────────────────────────────────────────


def test_artifact_kinds_are_a_closed_set_with_distinct_values():
    """All 8 reserved slots present; no duplicates; values are filesystem-safe."""
    values = [k.value for k in ArtifactKind]
    assert len(values) == 8, f"expected exactly 8 kinds, got {values}"
    assert len(set(values)) == 8, "duplicate ArtifactKind values"
    # Filesystem-safe: no slashes, no whitespace, lowercase + dashes only.
    for v in values:
        assert "/" not in v and " " not in v, f"unsafe kind value: {v!r}"

    # Pin the v1 names so future renames are caught — downstream tools
    # may grep these paths.
    assert ArtifactKind.API_LOG.value == "api-log"
    assert ArtifactKind.AGENT_LOG.value == "agent-log"
    assert ArtifactKind.SERVICE_LOG.value == "service-log"


# ── store construction: reserves layout up-front ───────────────────────


def test_store_creates_artifacts_root_and_readme(tmp_path):
    """At construction time, artifacts/ exists with a README — per-team
    subdirs appear lazily as artifacts land (no upfront reservation)."""
    store = ArtifactStore(tmp_path)

    root = tmp_path / "artifacts"
    assert root.is_dir()
    assert store.root == root
    assert (root / "README.md").is_file()
    # Layout is team-first now — no `<kind>/` subdirs at the top level.
    # No team dirs exist yet because nothing has been written.
    assert list(p.name for p in root.iterdir() if p.is_dir()) == []


def test_store_construction_is_idempotent(tmp_path):
    """Constructing twice over the same run dir doesn't blow up or rewrite
    the README. Lets GameMgr re-init defensively without losing state."""
    store = ArtifactStore(tmp_path)
    readme = tmp_path / "artifacts" / "README.md"
    original = readme.read_text()
    # Drop a sentinel via path_for so the second construction sees it.
    sentinel = store.path_for(
        ArtifactKind.API_LOG, team_id="team1", round_id=0, filename="sentinel.txt",
    )
    sentinel.write_text("keep me")

    ArtifactStore(tmp_path)  # second construction

    assert readme.read_text() == original
    assert sentinel.read_text() == "keep me"


# ── path_for: layout + lazy parent creation ────────────────────────────


def test_path_for_returns_expected_layout(tmp_path):
    """runs/<ts>/artifacts/<team_id>/round-<N>/<filename>.

    Kind is a closed-set tag for the caller but does not appear in the
    path — different kinds live side-by-side under the same round dir,
    disambiguated by filename convention.
    """
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.API_LOG,
        team_id="team1",
        round_id=3,
        filename="stream-defense.jsonl",
    )
    assert p == tmp_path / "artifacts" / "team1" / "round-3" / "stream-defense.jsonl"


def test_path_for_groups_all_kinds_under_one_round_dir(tmp_path):
    """All artifact kinds for the same (team, round) share one directory."""
    store = ArtifactStore(tmp_path)
    api_p = store.path_for(ArtifactKind.API_LOG, team_id="team1", round_id=1, filename="stream-defense.jsonl")
    agent_p = store.path_for(ArtifactKind.AGENT_LOG, team_id="team1", round_id=1, filename="agent-defense.log")
    svc_p = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=1, filename="docker.log")
    assert api_p.parent == agent_p.parent == svc_p.parent
    assert api_p.parent == tmp_path / "artifacts" / "team1" / "round-1"


def test_path_for_creates_parent_dirs_lazily(tmp_path):
    """Caller can write to the returned path without mkdir-ing first."""
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.AGENT_LOG,
        team_id="team2",
        round_id=7,
        filename="stderr.log",
    )
    assert p.parent.is_dir()
    # And the file is writable right away.
    p.write_text("hi")
    assert p.read_text() == "hi"


def test_path_for_across_multiple_rounds_and_teams_isolates_directories(tmp_path):
    """Per-team-per-round subdirs are independent — writes to one don't
    surface in another."""
    store = ArtifactStore(tmp_path)
    a = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=1, filename="x.log")
    b = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team2", round_id=1, filename="x.log")
    c = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=2, filename="x.log")
    a.write_text("A")
    b.write_text("B")
    c.write_text("C")
    assert a.read_text() == "A"
    assert b.read_text() == "B"
    assert c.read_text() == "C"


# ── escape rejection ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_filename",
    [
        "../etc/passwd",
        "../../../etc/passwd",
        "foo/../../etc/passwd",
        "subdir/../../escape",
    ],
)
def test_path_for_rejects_dotdot_in_filename(tmp_path, bad_filename):
    """`..` would escape the artifacts root — refuse it loudly rather
    than silently writing outside the run."""
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match=r"\.\."):
        store.path_for(
            ArtifactKind.EXPLOIT,
            team_id="team1",
            round_id=1,
            filename=bad_filename,
        )


def test_path_for_rejects_absolute_filename(tmp_path):
    """An absolute filename would silently bypass the kind/team/round prefix."""
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="relative"):
        store.path_for(
            ArtifactKind.API_LOG,
            team_id="team1",
            round_id=1,
            filename="/etc/passwd",
        )


def test_path_for_allows_nested_filename(tmp_path):
    """A nested filename like 'sub/file.log' is fine — it stays under the
    round dir. (Only `..` and absolute paths escape.)"""
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.API_LOG,
        team_id="team1",
        round_id=1,
        filename="sub/stream.jsonl",
    )
    p.write_text("ok")
    assert p == tmp_path / "artifacts" / "team1" / "round-1" / "sub" / "stream.jsonl"
    assert p.read_text() == "ok"


def test_path_for_rejects_non_enum_kind(tmp_path):
    """Passing a raw string instead of an ArtifactKind member must raise —
    otherwise the closed-set discipline silently degrades to free-form."""
    store = ArtifactStore(tmp_path)
    with pytest.raises(TypeError, match="ArtifactKind"):
        store.path_for(
            "api-log",  # type: ignore[arg-type]
            team_id="team1",
            round_id=1,
            filename="x.log",
        )
