"""Per-challenge prompts for the notes daemon.

Stage 1 placeholder: the real prompt builders still live in
[agents/prompts/notes.py](../../agents/prompts/notes.py). Stage 6 will
move them here and switch `agents/prompts/__init__.py`'s dispatcher to
load `challenges.notes.prompts` for the `notes` challenge.

For now this module just re-exports the existing builders so callers
that want to reach for "the notes prompts module" have a stable name
under the challenge directory.
"""

from __future__ import annotations

# Re-export from the legacy location until Stage 6 moves the bodies here.
from agents.prompts.notes import build_attack_prompt  # noqa: F401

# Notes is attack-only — no build_defense_prompt.
