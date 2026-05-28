"""Challenge registry — discovery via ChallengeSource list (DEFCON framework).

The pre-DEFCON registry was a hardcoded `dict[type_str, Service class]`.
With the framework, sources become the discovery primitive: each
`ChallengeSource` enumerates its own `ChallengeSpec`s and knows how
to materialize them.

Back-compat: the legacy `build(type_, **kwargs)` shim still works for
M5/M5.5/M5.6 call sites that pass a service type string (e.g. "notes").
It looks up the matching Service class via the source list and
instantiates it. Once the topology refactor migrates everything to
spec-driven deploy, the shim can retire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.service import Service

from challenges.notes import BuiltinNotesSource, NotesService

if TYPE_CHECKING:
    from challenges.source import ChallengeSource


# Default in-tree sources. External CTF adapters (nautilus, picoCTF, …)
# get appended here in Phase 3 once challenge_sources: is parsed from
# game.yaml. For Phase 2 only `notes` is wired through this list.
def default_sources() -> list["ChallengeSource"]:
    """The built-in sources always present in the registry."""
    return [BuiltinNotesSource()]


def sources_for(cfg) -> list["ChallengeSource"]:
    """Return every ChallengeSource needed to materialize `cfg.challenges`.

    Always includes the built-in notes source. For challenges whose name
    is prefixed by an external adapter key (e.g. `nautilus-<chal>`), the
    matching adapter is instantiated with the right `include=` list.

    Until the two-section `challenge_sources:` yaml schema lands, this is
    the single point of source discovery — main.py + GameMgr both call
    here so a new adapter (picoCTF, custom git) only needs one branch.
    """
    sources: list["ChallengeSource"] = list(default_sources())

    nautilus_includes = [
        c.name[len("nautilus-"):]
        for c in cfg.challenges
        if c.name.startswith("nautilus-")
    ]
    if nautilus_includes:
        # Lazy import — keep the docker SDK out of notes-only paths.
        from challenges.nautilus import NautilusSource
        sources.append(NautilusSource(include=nautilus_includes))

    return sources


def build_all_images(cfg) -> list[str]:
    """Build (or cache-hit) every image needed by `cfg.challenges`.

    Iterates `sources_for(cfg)` and calls `source.build_image(spec)` for
    each spec whose `name` matches an entry in `cfg.challenges`. Returns
    the list of resolved image tags.

    Idempotent: each adapter's `build_image` skips when the tag is already
    present locally. Safe to call from both `setup` and `start` paths.
    """
    wanted_names = {c.name for c in cfg.challenges}
    tags: list[str] = []
    for src in sources_for(cfg):
        for spec in src.list_challenges():
            if spec.name in wanted_names:
                tags.append(src.build_image(spec))
    return tags


# ── Legacy back-compat shim ───────────────────────────────────────────
#
# game_mgr.deploy_challenges still calls `build(chcfg.type, port=...)`.
# We keep that signature working by mapping the type string to a Service
# class via the source list's specs. This shim stays until Phase 3
# (gamemgr/config.py + game_mgr) switches to spec-driven materialization.

# Hardcoded fallback map keyed by source/spec name → Service class.
# Sources whose specs reference a `service_class` metadata key get added
# at import time below.
_LEGACY_SERVICE_CLASSES: dict[str, type[Service]] = {
    "notes": NotesService,
}


def build(type_: str, **kwargs) -> Service:
    """Legacy: instantiate a Service by its name (e.g. "notes").

    Phase 2-era shim. Prefer iterating `default_sources()` and
    materializing specs once the topology refactor lands.
    """
    if type_ not in _LEGACY_SERVICE_CLASSES:
        raise KeyError(
            f"unknown challenge type: {type_!r}; "
            f"known: {sorted(_LEGACY_SERVICE_CLASSES)}"
        )
    return _LEGACY_SERVICE_CLASSES[type_](**kwargs)


# Back-compat: the old module-level REGISTRY dict is still accessed by
# some tests. Keep it as an alias to the legacy class map so nothing
# breaks while the framework lands.
REGISTRY = _LEGACY_SERVICE_CLASSES
