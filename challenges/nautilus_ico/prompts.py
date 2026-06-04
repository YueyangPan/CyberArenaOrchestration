"""Per-challenge prompts for nautilus-ico.

Stage 1 placeholder: the real prompt builders still live in
[agents/prompts/nautilus_ico.py](../../agents/prompts/nautilus_ico.py).
Stage 6 will move them here and switch
`agents/prompts/__init__.py`'s dispatcher to load
`challenges.nautilus_ico.prompts` for the `nautilus-ico` challenge.
"""

from __future__ import annotations

# Re-export from the legacy location until Stage 6 moves the bodies here.
from agents.prompts.nautilus_ico import (  # noqa: F401
    build_attack_prompt,
    build_defense_prompt,
)
