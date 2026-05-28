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
    assert ArtifactKind.API_LOG.value == "api-logs"
    assert ArtifactKind.AGENT_LOG.value == "agent-logs"
    assert ArtifactKind.SERVICE_LOG.value == "service-logs"


# ── store construction: reserves layout up-front ───────────────────────


def test_store_creates_artifacts_root_with_all_kind_subdirs(tmp_path):
    """At construction time, artifacts/ exists with one subdir per kind +
    a README — so the layout is visible even before any artifact lands."""
    store = ArtifactStore(tmp_path)

    root = tmp_path / "artifacts"
    assert root.is_dir()
    assert store.root == root

    for kind in ArtifactKind:
        assert (root / kind.value).is_dir(), f"missing reserved subdir for {kind.value}"

    assert (root / "README.md").is_file()


def test_store_construction_is_idempotent(tmp_path):
    """Constructing twice over the same run dir doesn't blow up or rewrite
    the README. Lets GameMgr re-init defensively without losing state."""
    ArtifactStore(tmp_path)
    readme = tmp_path / "artifacts" / "README.md"
    original = readme.read_text()
    # Drop a sentinel file so we can detect any clobber.
    sentinel = tmp_path / "artifacts" / "api-logs" / "sentinel.txt"
    sentinel.write_text("keep me")

    ArtifactStore(tmp_path)  # second construction

    assert readme.read_text() == original
    assert sentinel.read_text() == "keep me"


# ── path_for: layout + lazy parent creation ────────────────────────────


def test_path_for_returns_expected_layout(tmp_path):
    """runs/<ts>/artifacts/<kind>/<team_id>/round-<N>/<filename>."""
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.API_LOG,
        team_id="team1",
        round_id=3,
        filename="stream.jsonl",
    )
    assert p == tmp_path / "artifacts" / "api-logs" / "team1" / "round-3" / "stream.jsonl"


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
    assert p == tmp_path / "artifacts" / "api-logs" / "team1" / "round-1" / "sub" / "stream.jsonl"
    assert p.read_text() == "ok"
