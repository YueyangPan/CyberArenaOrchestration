"""Prompt-builder dispatcher.

Per-challenge attack + defense prompts live alongside the challenge
implementation under `challenges/<name>/prompts.py`. The dispatcher
routes by `service_name` (== Challenge.name) so the framework doesn't
bake in any specific challenge.

Adding a new challenge:
  1. Create `challenges/<name>/prompts.py` exporting `build_attack_prompt`
     and (optionally) `build_defense_prompt` with the signatures used here.
  2. Register the slug -> dotted module path in `_PROMPT_MODULES` below.
  3. If `build_defense_prompt` isn't supplied, the generic fallback
     (`agents.prompts._helpers.generic_defense_prompt`) is used — works
     for any FILE_AT_PATH challenge whose defense recipe is
     edit-source / restart_service / wait / report.

Keep prompts short and concrete — Claude in headless `-p` mode runs one
turn-loop and exits, so wandering exploration costs tokens without
helping. Spell out the *exact* commands the agent should run.
"""

from __future__ import annotations

import importlib
from typing import Any

from models.agent import AgentConfig

# Re-export so legacy `from agents.prompts import submit_block` callers
# keep working. New code should import from `agents.prompts._helpers`.
from ._helpers import (  # noqa: F401
    generic_defense_prompt,
    short_attack_cue,
    short_defense_cue,
    submit_block,
)


# slug (matches Challenge.name) -> dotted path of the per-challenge
# prompts module. Per-challenge bodies live next to the challenge code
# so everything for one challenge stays in one directory.
_PROMPT_MODULES: dict[str, str] = {
    "notes": "challenges.notes.prompts",
    "nautilus-ico": "challenges.nautilus_ico.prompts",
}


def _load_module(service_name: str | None) -> Any:
    """Return the per-challenge prompt module, or None if no override exists."""
    if service_name is None:
        return None
    dotted = _PROMPT_MODULES.get(service_name)
    if dotted is None:
        return None
    return importlib.import_module(dotted)


# ── Dispatch ──────────────────────────────────────────────────────────


def build_attack_prompt(
    cfg: AgentConfig,
    *,
    service_name: str | None = None,
    is_first: bool = True,
) -> str:
    """Return the attack-mode prompt for `service_name`.

    `is_first=True` (default) → full briefing for the first phase of
    the game on this team. `is_first=False` → short continuation cue;
    Claude's `--continue` carries all prior context from the first
    prompt so we don't re-send the topology / protocol crib.

    Falls back to the notes recipe when no service-specific module is
    registered — preserves the single-challenge smoke-test path.
    """
    if not is_first:
        return short_attack_cue(cfg)
    mod = _load_module(service_name) or _load_module("notes")
    return mod.build_attack_prompt(cfg)


def build_defense_prompt(
    cfg: AgentConfig,
    *,
    service_name: str,
    port: int,
    source_dir: str = "/srv/challenge",
    is_first: bool = True,
) -> str:
    """Return the defense-mode prompt for `service_name`.

    `is_first=True` (default) → full briefing for the first phase.
    `is_first=False` → short continuation cue (the full source-layout +
    SSH instructions live in Claude's `--continue` session memory).

    Per-challenge module may export `build_defense_prompt` to override
    the generic recipe — useful when the challenge wants to hint at
    specific mitigations. Most challenges can use the generic version.
    """
    if not is_first:
        return short_defense_cue(cfg, service_name=service_name, port=port)
    mod = _load_module(service_name)
    if mod is not None and hasattr(mod, "build_defense_prompt"):
        return mod.build_defense_prompt(
            cfg, service_name=service_name, port=port, source_dir=source_dir,
        )
    return generic_defense_prompt(
        cfg, service_name=service_name, port=port, source_dir=source_dir,
    )
