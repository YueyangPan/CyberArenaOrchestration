"""DEFCON: ChallengeSource ABC contract.

Parametrized over the two v1 adapters (notes + nautilus-ico) — keeps
the interface honest by exercising the same body against both. No
docker required for these assertions (we don't call build_image; that's
covered by test_dcf_nautilus_ico.py with real docker).

What we pin:
- list_challenges() returns a non-empty list of ChallengeSpec
- every spec.name is unique within the source
- mandatory fields are populated (name, source, port, image_tag, flag_mechanism)
- spec.metadata is a dict (opaque to framework but must be present)
- nautilus-specific: source_view set, restart_cmd + health_check metadata
"""

from __future__ import annotations

import pytest

from challenges.notes import BuiltinNotesSource
from challenges.nautilus import NautilusSource
from challenges.source import ChallengeSource, ChallengeSpec, FlagMechanism


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def notes_source() -> ChallengeSource:
    return BuiltinNotesSource()


@pytest.fixture
def nautilus_source() -> ChallengeSource:
    """Real NautilusSource backed by the vendored repo. Skips if vendor/
    isn't synced (lets the contract test still run in CI before the
    sync_nautilus.sh step)."""
    src = NautilusSource(include=["ico"])
    try:
        src.list_challenges()
    except FileNotFoundError as e:
        pytest.skip(f"nautilus vendor not synced: {e}")
    return src


@pytest.fixture(params=["notes", "nautilus"])
def source(request, notes_source, nautilus_source) -> ChallengeSource:
    return notes_source if request.param == "notes" else nautilus_source


# ── Contract ──────────────────────────────────────────────────────────


def test_list_challenges_returns_non_empty_specs(source):
    specs = source.list_challenges()
    assert isinstance(specs, list)
    assert len(specs) >= 1
    for s in specs:
        assert isinstance(s, ChallengeSpec)


def test_spec_names_are_unique_within_source(source):
    specs = source.list_challenges()
    names = [s.name for s in specs]
    assert len(set(names)) == len(names), f"duplicate names: {names}"


def test_mandatory_spec_fields_populated(source):
    """Every spec must have non-empty name/source/image_tag, a positive
    int port, and a FlagMechanism enum value."""
    for spec in source.list_challenges():
        assert isinstance(spec.name, str) and spec.name
        assert isinstance(spec.source, str) and spec.source
        assert isinstance(spec.port, int) and spec.port > 0
        assert isinstance(spec.image_tag, str) and spec.image_tag
        assert isinstance(spec.flag_mechanism, FlagMechanism)
        assert isinstance(spec.metadata, dict)


def test_spec_is_frozen_dataclass(source):
    """ChallengeSpec is `frozen=True` — accidental mutation downstream
    would be silently destabilizing. Attempt to mutate; expect FrozenInstanceError."""
    from dataclasses import FrozenInstanceError

    spec = source.list_challenges()[0]
    with pytest.raises(FrozenInstanceError):
        spec.port = 9999  # type: ignore[misc]


# ── Per-source specifics ──────────────────────────────────────────────


def test_notes_spec_uses_service_protocol_flag_mechanism(notes_source):
    """Notes plants via PUT over its own TCP protocol — not a file or env var."""
    specs = notes_source.list_challenges()
    notes_spec = next(s for s in specs if s.name == "notes")
    assert notes_spec.flag_mechanism == FlagMechanism.SERVICE_PROTOCOL
    # Notes has no defense surface; source_view is None (documented in plan §health).
    assert notes_spec.source_view is None


def test_nautilus_ico_spec_is_well_formed(nautilus_source):
    """ico spec must declare source_view, restart_cmd, health_check —
    everything `restart_service` + the health chain rely on."""
    specs = nautilus_source.list_challenges()
    ico = next(s for s in specs if s.name == "nautilus-ico")
    assert ico.source == "nautilus"
    assert ico.port == 4265
    assert ico.flag_mechanism == FlagMechanism.FILE_AT_PATH
    assert ico.metadata.get("flag_path") == "/flag"
    assert ico.metadata.get("restart_cmd") == "/arena/restart.sh"
    # Source view is the subpath copied into the image at build time.
    assert ico.source_view is not None
    assert str(ico.source_view) == "service"
    # Health-check block: process_pattern (for PROCESS-level pgrep) +
    # checker_command (for CHECKER-level cheese-path detection).
    hc = ico.metadata.get("health_check")
    assert hc is not None
    assert hc.get("process_pattern")
    assert hc.get("checker_command") == "/arena/checker.sh"
    # upstream_name is needed by build_image to find the vendor dir.
    assert ico.metadata.get("upstream_name") == "ico"
    # Image tag follows the cyberarena/chal-nautilus-<upstream>:<pin> convention.
    assert ico.image_tag.startswith("cyberarena/chal-nautilus-ico:")


def test_nautilus_missing_vendor_raises_with_actionable_message(tmp_path):
    """If vendor/ isn't synced, list_challenges() must point the operator
    at scripts/sync_nautilus.sh rather than failing with a cryptic
    pathlib error."""
    src = NautilusSource(repo_ref=tmp_path / "does-not-exist", include=["ico"])
    with pytest.raises(FileNotFoundError, match="sync_nautilus.sh"):
        src.list_challenges()


def test_nautilus_unknown_include_name_raises(tmp_path, monkeypatch):
    """include=['typo'] with no matching framework dir must fail loudly."""
    # Use the real vendor path; only the framework dir is missing.
    src = NautilusSource(include=["definitely-not-a-real-chal-xyz"])
    try:
        with pytest.raises(FileNotFoundError, match="no framework dir|no upstream dir"):
            src.list_challenges()
    except FileNotFoundError as e:
        if "sync_nautilus.sh" in str(e):
            pytest.skip("nautilus vendor not synced")
        raise


# ── HealthLevel ordering (used by the cheese-path test) ───────────────


def test_health_level_enum_has_all_four_values():
    """The plan's whole defense-soundness story hinges on 4 levels."""
    from challenges.source import HealthLevel

    levels = {l.value for l in HealthLevel}
    assert levels == {"process", "tcp", "protocol", "checker"}
