"""Challenge registry — maps `type` strings in game.yaml to adapter classes."""

from __future__ import annotations

from orchestrator.service import Service

from challenges.notes import NotesService

REGISTRY: dict[str, type[Service]] = {
    "notes": NotesService,
    # add new challenges here: "blog": BlogService, ...
}


def build(type_: str, **kwargs) -> Service:
    if type_ not in REGISTRY:
        raise KeyError(f"unknown challenge type: {type_!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[type_](**kwargs)