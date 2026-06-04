"""Game-simple notes challenge — attack-only TCP daemon.

Composition under `challenges.interface.Challenge`:
  vulbox             — generic prod-base image; ports={"service": 5000}
  functionality_test — 4-level chain (process / tcp / protocol / checker)
  flag_handler       — PUT <flag> via the notes daemon's own protocol
  restart_handler    — None (attack-only)
  docs               — intro + func_test_spec; agent_prompts wired in Stage 6

Public surface is the `NotesChallenge` class; callers import it
through the registry, not directly.
"""

from .challenge import NotesChallenge

# Legacy v1 surface kept importable until Stage 8 deletes the source.py /
# ChallengeSource path entirely. `_legacy` was the old top-level
# `challenges/notes.py`; moved into the package so the directory + module
# don't shadow each other.
from ._legacy import (  # noqa: F401
    REMOTE_DIR,
    SERVER_SCRIPT,
    BuiltinNotesSource,
    NotesService,
)

__all__ = ["NotesChallenge", "BuiltinNotesSource", "NotesService"]
