"""Game-simple notes challenge — attack-only TCP daemon.

Composition under `challenges.interface.Challenge`:
  vulbox             — cyberarena/chal-notes:<tag>; ports={"service": 5000}
  functionality_test — 4-level chain (process / tcp / protocol / checker)
  flag_handler       — PUT <flag> via the notes daemon's own protocol
  restart_handler    — None (attack-only)
  docs               — intro + func_test_spec; agent_prompts wired in Stage 6

Public surface is the `NotesChallenge` class; callers import it through
the registry, not directly.
"""

from .challenge import NotesChallenge

__all__ = ["NotesChallenge"]
